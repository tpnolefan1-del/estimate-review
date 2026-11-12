#!/usr/bin/env python3
"""
Estimate Review Prototype for Ben's Body Works
Extracts data from CCC ONE PDFs and suggests missed operations.
"""

import pdfplumber
import re
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from build_sheet_parser import parse_build_sheet

class EstimateParser:
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.data = {
            "ro_number": None,
            "vehicle": None,
            "vin": None,
            "year_make_model": None,
            "labor_rates": {},
            "line_items": [],
            "totals": {},
            "raw_text": ""
        }
    
    def parse(self) -> Dict[str, Any]:
        with pdfplumber.open(self.pdf_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text += text + "\n"
            self.data["raw_text"] = full_text
        
        self._extract_header()
        self._extract_labor_rates()
        self._extract_line_items()
        self._extract_totals()
        return self.data
    
    def _extract_header(self):
        text = self.data["raw_text"]
        # RO Number
        m = re.search(r"RO Number:\s*(\d+)", text)
        if m:
            self.data["ro_number"] = m.group(1)
        
        # Vehicle description (usually after VEHICLE)
        m = re.search(r"VEHICLE\s*\n(20\d{2}\s+[A-Z][A-Z0-9\s]+)", text)
        if m:
            self.data["year_make_model"] = m.group(1).strip().split("\n")[0]
        
        # VIN
        m = re.search(r"VIN:\s*([A-Z0-9]{17})", text)
        if m:
            self.data["vin"] = m.group(1)
    
    def _extract_labor_rates(self):
        text = self.data["raw_text"]
        # Look for typical rate lines
        rates = {}
        for line in text.split("\n"):
            if "Body Labor" in line and "@" in line:
                m = re.search(r"\$\s*([\d.]+)\s*/hr", line)
                if m:
                    rates["body"] = float(m.group(1))
            if "Paint Labor" in line and "@" in line:
                m = re.search(r"\$\s*([\d.]+)\s*/hr", line)
                if m:
                    rates["paint"] = float(m.group(1))
            if "Mechanical Labor" in line and "@" in line:
                m = re.search(r"\$\s*([\d.]+)\s*/hr", line)
                if m:
                    rates["mechanical"] = float(m.group(1))
            if "Paint Supplies" in line and "@" in line:
                m = re.search(r"\$\s*([\d.]+)\s*/hr", line)
                if m:
                    rates["paint_supplies"] = float(m.group(1))
        self.data["labor_rates"] = rates or {"body": 65.0, "paint": 65.0, "mechanical": 85.0, "paint_supplies": 40.0}
    
    def _extract_line_items(self):
        # Simplified line item extraction - focuses on operations
        lines = self.data["raw_text"].split("\n")
        items = []
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Section headers (all caps, no numbers at start usually)
            if re.match(r"^[A-Z][A-Z\s&]+$", line) and len(line) > 8 and "PAGE" not in line:
                current_section = line
                continue
            
            # Line items with operation
            # Pattern: number, optional flags, operation (R&I, Repl, Rpr, etc.), description
            m = re.match(r"^(\d+)\s+(?:[S0-9\*]+\s+)?(R&I|Repl|Rpr|Refn|Blnd|Algn|Subl|O/H|Add for|Overlap)\s+(.+)", line)
            if m:
                items.append({
                    "line": int(m.group(1)),
                    "operation": m.group(2),
                    "description": m.group(3).strip()[:100],
                    "section": current_section
                })
        
        self.data["line_items"] = items
    
    def _extract_totals(self):
        text = self.data["raw_text"]
        m = re.search(r"Grand Total\s+([\d,]+\.\d{2})", text)
        if m:
            self.data["totals"]["grand_total"] = float(m.group(1).replace(",", ""))


class MissedOpsAnalyzer:
    """Rule-based + domain knowledge suggestions for missed operations."""
    
    def __init__(self, estimate_data: Dict, build_sheet_data: Optional[Dict] = None):
        self.data = estimate_data
        self.build_sheet = build_sheet_data or {}
        self.suggestions = []
        self.rates = estimate_data.get("labor_rates", {})
        self.body_rate = self.rates.get("body", 65.0)
        self.paint_rate = self.rates.get("paint", 65.0)
        self.mech_rate = self.rates.get("mechanical", 85.0)
    
    def _has_option(self, keywords: List[str]) -> bool:
        """Check if build sheet confirms an option is installed."""
        if not self.build_sheet:
            return True  # if no build sheet, don't filter
        opts = " ".join(self.build_sheet.get("installed_options", [])).lower()
        return any(kw.lower() in opts for kw in keywords)

    def analyze(self) -> List[Dict]:
        self.suggestions = []
        items = self.data.get("line_items", [])
        ops_text = " ".join([f"{i['operation']} {i['description']}" for i in items]).lower()
        
        # Rule 1: Bumper related R&I
        if any(x in ops_text for x in ["bumper cover", "bumper assy", "o/h bumper"]):
            if "headlamp" not in ops_text and "lamp" not in ops_text:
                self._add("Remove & Install", "R&I Headlamp assemblies (both sides)", 0.5, 
                          "Upper tabs of bumper cover typically sit behind headlamps; lamps must come off to service cover.",
                          trigger="Bumper cover R&I")
            if "energy absorber" not in ops_text and "impact bar" not in ops_text:
                self._add("Remove & Install", "R&I Front energy absorber / impact bar", 0.4,
                          why="Often required when bumper cover is replaced; check if present on vehicle.",
                          trigger="Bumper cover R&I")
        
        # Rule 2: Scans / Diagnostics — use realistic sublet pricing
        if "scan" not in ops_text and "diagnostic" not in ops_text:
            self._add("Pre/Post Scans", "Pre-repair diagnostic scan", 0.0, 0.0,
                      "Required on modern vehicles to capture loss-related DTCs before repairs. Typical invoice ~$50–$130.")
            self.suggestions[-1]["amount"] = 75.00
            self.suggestions[-1]["description"] += " — est. $75"
            self.suggestions[-1]["why"] = "Nissan (Nov 2025) requires pre- and post-repair scans on all 2008+ vehicles using the CONSULT diagnostic tool only. Honda (July 2025), Ford (Oct 2025), Kia, Toyota CRIB #191, FCA, and GM also require pre/post scans. Not all DTCs illuminate a warning light. Typical invoice $50–$130."
            self.suggestions[-1]["trigger"] = "Any collision repair"
            
            self._add("Pre/Post Scans", "Post-repair diagnostic scan", 0.0, 0.0,
                      "Toyota CRIB #191, FCA, and GM require a post-repair diagnostic scan to confirm all systems are functioning properly and to identify any systems that need repair, reprogramming, or initialization. GM requires retesting after all repairs are complete to verify faults have been repaired and no new faults were introduced. Typical invoice $50–$130.",
                      trigger="Any collision repair")
            self.suggestions[-1]["amount"] = 100.00
            self.suggestions[-1]["description"] += " — est. $100"
        
        # Rule 3: Battery disconnect
        if "battery" not in ops_text and "disconnect" not in ops_text:
            if any(x in ops_text for x in ["srs", "air bag", "restraint", "module"]):
                self._add("Miscellaneous", "Disconnect / reconnect battery", 0.3, self.mech_rate,
                          "Required before working on the SRS or electronic modules to prevent accidental deployment or system faults.",
                          trigger="SRS / air bag work")
                # Power window initialization is often required after battery disconnect
                if "window" not in ops_text and "initialization" not in ops_text and "reinitialize" not in ops_text:
                    self._add("Repair Procedures", "Power Window Initialization", 0.0, 0.0,
                              "Toyota requires power window control system initialization when a door window regulator, motor, door glass, or glass run is reinstalled or replaced. Functions such as auto up/down, jam protection, remote control, and key-linked functions will not operate if initialization is not performed. Also required after battery disconnect on many vehicles.")
                    self.suggestions[-1]["amount"] = 125.00
                self.suggestions[-1]["description"] += " — est. $125"
        
        # Power window initialization after battery disconnect (even if battery line is already present)
        if "battery" in ops_text and "window" not in ops_text and "initialization" not in ops_text and "reinitialize" not in ops_text:
            self._add("Repair Procedures", "Power Window Initialization", 0.0, 0.0,
                      "Disconnecting the 12V battery can erase the windows' stored travel limits and anti-pinch protection. The power window control unit must be re-initialized so one-touch operation and anti-pinch reverse function correctly.",
                      trigger="Battery disconnect")
            self.suggestions[-1]["amount"] = 125.00
            self.suggestions[-1]["description"] += " — est. $125"
        
        # Rule: Post-collision SRS / air bag inspection
        if any(x in ops_text for x in ["srs", "air bag", "restraint", "airbag"]):
            if "inspection" not in ops_text and "inspect" not in ops_text:
                self._add("Repair Procedures", "Post-collision SRS / air bag inspection", 0.5, self.mech_rate,
                          "Toyota/Lexus and other OEMs call for post-collision air bag inspections even in non-deployment crashes. Shops have found deformed passenger air bags and tweaked mounting brackets with no dash warning lights or DTCs. Had the inspection not been performed, the damage would not have been found.",
                          trigger="SRS / air bag work")
        if "seat belt" not in ops_text and "seatbelt" not in ops_text and "belt inspection" not in ops_text:
            self._add("Repair Procedures", "Post-collision seat belt inspection", 0.5, self.mech_rate,
                      "GM, FCA, Toyota/Lexus, and most OEMs require inspection of all seat belts after any collision before returning the vehicle to the customer. Real-world inspections have uncovered deformed air bags and tweaked mounting brackets with no warning lights or DTCs. GM requires replacement of any seat belt system in use during a deployment event or if components are torn, worn, or damaged. Always inspect and replace the entire system if needed.",
                      trigger="Any collision repair")
        # Rule: GM ADAS bumper fascia — substrate repair not approved
        if any(x in ops_text for x in ["bumper", "fascia"]) and any(x in ops_text for x in ["rpr", "repair", "substrate", "filler"]):
            if self._has_option(["radar", "acc", "adas", "parking", "blind"]) or "adas" in ops_text:
                self._add("Parts Differences", "ADAS bumper fascia substrate repair not approved — replace", 0.0, 0,
                          "Ford (Oct 2025) and GM strictly prohibit aftermarket, reconditioned, or salvage bumpers/fascias on ADAS-equipped vehicles. Ford limits topcoat refinish to ≤12 mils and does not approve fillers, reinforcement tape, plastic welding, or sonic-weld repairs on ADAS bumper fascias. Toyota CRIB #201 requires special precautions on 77 GHz radar vehicles. Only genuine bumper assemblies are approved.",
                          trigger="Bumper fascia repair on ADAS-equipped vehicle")

        # Rule: Used / salvaged SRS components not approved
        if any(x in ops_text for x in ["srs", "air bag", "airbag", "restraint"]) and any(x in ops_text for x in ["salvage", "used", "recycled", "lkq", "aftermarket", "a/m"]):
            self._add("Parts Differences", "Used / salvaged SRS components not approved", 0.0, 0,
                      "GM strictly prohibits the use of any used, salvaged, or imitation SRS components. Only new, genuine GM warranted parts should be used. Proper operation of the air bag system requires new GM parts. Never use air bag parts from another vehicle or source.",
                      trigger="Salvage / aftermarket SRS component on estimate")
        if any(x in ops_text for x in ["intrusion", "door beam", "side beam"]) and any(x in ops_text for x in ["rpr", "repair", "straighten"]):
            self._add("Parts Differences", "Intrusion beam must be replaced (not repaired)", 0.0, 0,
                      "Toyota CRIB #161 states intrusion beam repair is prohibited. Intrusion beams are designed to absorb and dissipate collision energy at 100% strength in their original shape. Damaged intrusion beams require complete door replacement.",
                      trigger="Intrusion beam repair on estimate")
        # Rule: Bumper reinforcement repair is prohibited
        if any(x in ops_text for x in ["bumper reinforcement", "bumper beam", "impact bar"]) and any(x in ops_text for x in ["rpr", "repair", "straighten"]):
            self._add("Parts Differences", "Bumper reinforcement must be replaced (not repaired)", 0.0, 0,
                      "Toyota CRIB #161 states bumper reinforcement repair is prohibited. Bumper reinforcements are designed to absorb, channel, and dissipate collision energy. If damaged and repaired, they will no longer perform as intended. Damaged bumper reinforcements require replacement.",
                      trigger="Bumper reinforcement repair on estimate")
        if any(x in ops_text for x in ["rail", "pillar", "rocker", "reinforcement", "intrusion"]) and any(x in ops_text for x in ["rpr", "repair", "straighten"]):
            if "repl" not in ops_text:
                self._add("Parts Differences", "Structural repair not OEM-approved — replace instead", 0.0, 0,
                          "Toyota/Lexus CRIB #176 and CRIB #161 prohibit heating HSS/UHSS and require kinked structural members to be replaced. GM guidance on Ultra High Strength Steel (780 MPa+) states repair is not recommended. CRIB #194 allows sectioning only on non-structural mild steel outer panels; structural, inner, or non-mild-steel components require full replacement unless a sectioning joint is specifically stated in the body repair manual. Most OEMs require damaged or kinked structural members to be replaced rather than repaired.",
                          trigger="Structural member repair (rail / pillar / rocker)")
        if any(x in ops_text for x in ["glass", "sunroof", "weld", "section", "quarter", "door shell"]):
            if "water test" not in ops_text and "leak test" not in ops_text and "water leak" not in ops_text:
                self._add("Repair Procedures", "Water leak test", 0.5, self.body_rate,
                          "Once glass, a sunroof, or a welded panel is put back together, a water test confirms the seals actually keep water out. It's how you catch a leak in the shop instead of having the customer find it the first time it rains.",
                          trigger="Glass / sunroof / welded panel work")
        if any(x in ops_text for x in ["a/m", "aftermarket", "reman", "remanufactured", "lkq", "salvage"]):
            if "re-fit" not in ops_text and "refit" not in ops_text and "re fit" not in ops_text:
                self._add("Repair Procedures", "Re-fit labor (aftermarket / reman parts)", 0.5, self.body_rate,
                          "The published replace time assumes a new OEM part, and pre-fitment does not apply to an aftermarket or remanufactured part, so fitting one is work outside that time. MOTOR does not include a 2nd fitment of a component to the vehicle.",
                          trigger="Aftermarket / remanufactured part on estimate")
        if ("door" in ops_text and "repl" in ops_text) or ("door" in ops_text and any(x in ops_text for x in ["detrim", "r&i", "remove", "refn", "refinish"])):
            if "watershield" not in ops_text and "water shield" not in ops_text and "butyl" not in ops_text:
                self._add("Miscellaneous", "Door Watershield Sealant", 0.0, 0.0,
                          "The watershield is bedded in a strip of butyl sealant that tears when the sheet is peeled off, so it needs new sealant to seal the door again.",
                          trigger="Door shell replacement or door detrim/removal for refinish")
                self.suggestions[-1]["amount"] = 24.00
                self.suggestions[-1]["description"] += " — est. $24"
        if "quarter" in ops_text and "repl" in ops_text:
            if "hem flange" not in ops_text and "rolled hem" not in ops_text:
                self._add("Repair Procedures", "Rolled hem flange verification (quarter panel)", 0.0, 0,
                          "I-CAR notes that some vehicles have outer quarter panels with rolled hem flanges. Check the OEM Partial Part Replacement Search (red Outer Uniside box) to confirm. Recycled outer quarter panels with rolled hem flanges require special consideration. Starting with 2015 model year vehicles, the note will state whether the vehicle has, does not have, or has not yet been identified for a rolled hem flange.")
        if any(x in ops_text for x in ["radar", "camera", "sensor", "adas", "blind", "park assist", "sonar"]):
            if "aftermarket" in ops_text or "a/m" in ops_text or "salvage" in ops_text or "recycled" in ops_text or "lkq" in ops_text:
                self._add("Parts Differences", "Genuine OEM ADAS component required", 0.0, 0,
                          "Nissan (Jan 2026) strictly prohibits the use of salvaged, aftermarket, or secondary-market radars, cameras, and sensors. Only new Genuine Nissan OE parts are approved. GM, Toyota, FCA, and most OEMs prohibit non-genuine ADAS components. Non-genuine parts can compromise calibration accuracy and vehicle safety performance.")
            self._add("Parts Differences", "One-time-use clips and retainers (bumper/fender)", 0.0, 0,
                      "Toyota CRIB #188 and FCA specify that non-reusable parts (clips, fasteners, emblems, grommets, garnishes, etc.) are designed to be installed only once. If removed, they must be replaced and not reused. Estimate often undercounts quantity or omits them.",
                      trigger="Bumper / fender / door work")
        
        # Rule 5: Corrosion protection / materials
        materials_added = False
        # Specific seam sealer + cavity wax for common panel replacements
        # Matches the style shown in estimates (New Door/Hood/Trunk/Liftgate Seam Sealer and Cavity Wax)
        panel_sealer_map = [
            ("door", "New Door Seam Sealer and Cavity Wax", 2.0),
            ("hood", "New Hood Seam Sealer and Cavity Wax", 2.0),
            ("trunk", "New Deck Lid Seam Sealer and Cavity Wax", 1.5),
            ("deck lid", "New Deck Lid Seam Sealer and Cavity Wax", 1.5),
            ("liftgate", "New Liftgate Seam Sealer and Cavity Wax", 2.0),
            ("lift gate", "New Liftgate Seam Sealer and Cavity Wax", 2.0),
        ]
        # Check each line item so the panel and "repl" must be on the same line
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            for keyword, description, hours in panel_sealer_map:
                if keyword in line_text and "repl" in line_text:
                    if "seam sealer" not in ops_text and "cavity wax" not in ops_text:
                        self._add("Miscellaneous", description, hours, self.body_rate,
                                  "Required on new replacement panels. Seam sealer restores the factory water barrier and cavity wax protects the interior metal from corrosion. Current 3M pricing: seam sealer ~$96–$101, cavity wax ~$64.")
                        materials_added = True
                        break

        if any(x in ops_text for x in ["door", "quarter", "rocker", "pillar", "rail", "apron", "floor"]):
            if "panel bond" not in ops_text and "structural adhesive" not in ops_text:
                self._add("Miscellaneous", "Panel bond / structural adhesive (3M 08115)", 0.0, 0.0,
                          "Required for many panel replacements. 3M Panel Bonding Adhesive retail ~$95–$102/cartridge (current 3M pricing).")
                self.suggestions[-1]["amount"] = 98.00
                self.suggestions[-1]["description"] += " — est. $62"
                materials_added = True
            if "weld" in ops_text and "weld-thru" not in ops_text and "weld thru" not in ops_text:
                self._add("Miscellaneous", "Weld-thru primer (3M 05917)", 0.0, 0.0,
                          "Required on bare metal before welding. 3M Weld-Thru Coating II retail ~$86/can (current 3M pricing).")
                self.suggestions[-1]["amount"] = 86.00
                self.suggestions[-1]["description"] += " — est. $86"
                materials_added = True
            # Additional materials from 3M invoices
            if "nozzle" not in ops_text and "mixing" not in ops_text:
                self._add("Miscellaneous", "3M Static Mixing Nozzle (08194)", 0.0, 0.0,
                          "Required for seam sealer and panel bond cartridges. Current 3M retail ~$5.23 each.")
                self.suggestions[-1]["amount"] = 5.23
                self.suggestions[-1]["description"] += " — est. $5.23"
            if "undercoating" not in ops_text and "under coat" not in ops_text:
                self._add("Miscellaneous", "3M Rubberized Undercoating (08883)", 0.0, 0.0,
                          "Required on underbody and wheel well areas after repair. Current 3M retail ~$44.28/can.")
                self.suggestions[-1]["amount"] = 44.28
                self.suggestions[-1]["description"] += " — est. $44.28"
            if "dampen" not in ops_text and "deadener" not in ops_text and "nvh" not in ops_text:
                self._add("Miscellaneous", "3M Dampening Material", 0.0, 0.0,
                          "Required to restore NVH (noise/vibration/harshness) foam and dampening after panel replacement. Current 3M retail ~$130.93.")
                self.suggestions[-1]["amount"] = 130.93
                self.suggestions[-1]["description"] += " — est. $130.93"
        
        # Rule: Wet-sand, de-nib, and precision buff for black finishes
        if any(x in ops_text for x in ["refn", "refinish", "blend", "blnd", "clear coat", "paint"]):
            is_black = "black" in ops_text
            if is_black and "wet sand" not in ops_text and "denib" not in ops_text and "de-nib" not in ops_text and "buff" not in ops_text:
                self._add("Repair Procedures", "Wet-sand, de-nib & precision buff (black)", 0.8, self.paint_rate,
                          "Required on black finishes. Black shows every imperfection, so wet-sanding, de-nibbing, and a precision buff are needed for a proper finish.")

        # Rule: Tint Color time for non-black finishes
        if any(x in ops_text for x in ["refn", "refinish", "blend", "blnd", "clear coat", "paint"]):
            color_opts = " ".join(self.build_sheet.get("installed_options", [])).lower() if self.build_sheet else ""
            is_black = ("black" in ops_text and "not black" not in ops_text) or "black" in color_opts
            if not is_black and "tint" not in ops_text:
                self._add("Repair Procedures", "Tint color (color match)", 0.5, self.paint_rate,
                          "Required on non-black finishes. Time is needed to mix and adjust the color so the repair blends with the surrounding panels.",
                          trigger="Refinish / blend operation")

        # Rule: Spray-out cards for color matching (any color other than straight black)
        if any(x in ops_text for x in ["refn", "refinish", "blend", "blnd", "clear coat", "paint"]):
            color_opts = " ".join(self.build_sheet.get("installed_options", [])).lower() if self.build_sheet else ""
            is_black = ("black" in ops_text and "not black" not in ops_text) or "black" in color_opts
            if not is_black and "spray-out" not in ops_text and "spray out" not in ops_text and "sprayout" not in ops_text:
                self._add("Repair Procedures", "Spray-out cards (color match)", 0.5, self.paint_rate,
                          "Before any color touches the car, the painter sprays test cards to confirm the mixed color actually matches under the booth lights. It is the step that prevents repainting a whole panel because the blend came out off. CCC lists this as a separate, billable operation (Test spray-out panel — Not Included).",
                          trigger="Refinish / blend operation")

        # Rule: Let-Down Panel for 3-stage / tri-coat finishes
        is_three_stage = self._has_option(["three stage", "tri-coat", "tri coat", "tricoat"]) or "three stage" in ops_text or "tri-coat" in ops_text
        if is_three_stage and "let-down" not in ops_text and "letdown" not in ops_text and "let down" not in ops_text:
            self._add("Repair Procedures", "Let-Down Panel (3-stage / tri-coat)", 0.5, self.paint_rate,
                      "Required first step on tri-coat / 3-stage finishes. A let-down panel shows how many midcoat layers are needed so the repair matches the vehicle.",
                      trigger="3-stage / tri-coat refinish")

        # Blend-specific triggers
        # Blending the right rear door
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if ("rear door" in line_text or "rt door" in line_text or "right door" in line_text) and any(x in line_text for x in ["blnd", "blend"]):
                if "feather" not in ops_text and "prime" not in ops_text:
                    self._add("Repair Procedures", "Feather, Prime & Block (blend right rear door)", 0.5, self.paint_rate,
                              "Blending the right rear door requires feather, prime, and block to properly prepare the blend edge so the clear coat and color transition cleanly into the adjacent panel.",
                              trigger="Blend right rear door")
                break
        # Blending the roof rail or A-pillar
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if any(x in line_text for x in ["roof rail", "a-pillar", "a pillar", "aperture"]) and any(x in line_text for x in ["blnd", "blend"]):
                if "feather" not in ops_text and "prime" not in ops_text:
                    self._add("Repair Procedures", "Feather, Prime & Block (blend roof rail / A-pillar)", 0.5, self.paint_rate,
                              "Blending the roof rail or A-pillar requires feather, prime, and block to properly prepare the blend edge so the clear coat and color transition cleanly.",
                              trigger="Blend roof rail or A-pillar")
                break

        # Quarter panel replacement / sectioning triggers
        quarter_repl = False
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if "quarter" in line_text and any(x in line_text for x in ["repl", "section", "rpr"]):
                quarter_repl = True
                break
        if quarter_repl:
            if "headliner" not in ops_text:
                self._add("Repair Procedures", "R&I Headliner (quarter panel weld zone)", 3.2, self.body_rate,
                          "Required for access when sectioning or replacing a quarter panel. The headliner must be removed to protect it from weld heat and to reach the roof rail / sail panel weld zone.",
                          trigger="Quarter panel replacement / sectioning")
            if "air bag" not in ops_text and "airbag" not in ops_text and "roof rail" not in ops_text:
                self._add("Repair Procedures", "R&I Roof air bag (side of quarter replacement)", 1.0, self.body_rate,
                          "Required on the side of the quarter panel replacement. The roof rail air bag must be removed to protect it from weld heat and to access the sectioning joint. Follow OEM one-time-use fastener and torque requirements.",
                          trigger="Quarter panel replacement / sectioning")
            # Rear AC on vehicles equipped with rear climate control
            if self._has_option(["rear ac", "rear a/c", "rear climate", "tri-zone", "tri zone"]) or "rear ac" in ops_text:
                if "evacuation" not in ops_text and "recharge" not in ops_text and "evacuate" not in ops_text:
                    self._add("Repair Procedures", "Evacuate and recharge rear AC system", 1.5, self.mech_rate,
                              "Required when the rear AC unit is removed for access on a quarter panel replacement. The system must be evacuated before disconnecting lines and recharged after reassembly.",
                              trigger="Quarter panel replacement + rear AC equipped")
                if "rear ac" not in ops_text and "rear a/c" not in ops_text:
                    self._add("Repair Procedures", "R&I Rear AC unit (quarter panel access)", 1.8, self.mech_rate,
                              "Required for access when replacing the quarter panel on the side of the rear AC unit. The unit sits against the quarter and must be removed to reach the weld zone.",
                              trigger="Quarter panel replacement + rear AC equipped")

            # Additional OEMPro-style items for quarter panel replacement
            if "blend" not in ops_text and "blnd" not in ops_text:
                self._add("Repair Procedures", "Blend Right Rear Door Shell", 1.0, self.paint_rate,
                          "Fresh paint on the quarter panel rarely matches the aged factory finish on the panel beside it, so the color is blended into the rear door to make the change between them invisible. CCC lists blending into adjacent panels as not included.",
                          trigger="Quarter panel replacement")
                self._add("Repair Procedures", "Blend Right Aperture Panel", 1.5, self.paint_rate,
                          "The color is blended into the aperture panel (sail panel / roof rail area) and the whole panel is cleared to match the quarter panel being replaced. CCC lists blending into adjacent panels as not included.",
                          trigger="Quarter panel replacement")
            if "overhead" not in ops_text and "console" not in ops_text:
                self._add("Repair Procedures", "R&I Overhead Console", 0.3, self.body_rate,
                          "Required for access when removing the headliner or curtain air bag on a quarter panel replacement.",
                          trigger="Quarter panel replacement")
            if "kick panel" not in ops_text and "kickpanel" not in ops_text:
                self._add("Repair Procedures", "R&I Kick Panel (side of quarter)", 0.2, self.body_rate,
                          "Required for access to disconnect the curtain air bag wiring harness on the side of the quarter panel replacement.",
                          trigger="Quarter panel replacement")
            if "roof rail" not in ops_text and "roof rails" not in ops_text:
                self._add("Repair Procedures", "R&I Roof Rails", 0.8, self.body_rate,
                          "Required for access when sectioning the quarter panel and to protect the roof rails from weld heat and refinish overspray.",
                          trigger="Quarter panel replacement")
            if "energy absorber" not in ops_text:
                self._add("Repair Procedures", "R&I Rear Energy Absorber", 0.3, self.body_rate,
                          "Required for access when replacing the rear quarter or rear bumper area. The energy absorber sits behind the bumper cover and must be removed to reach the weld zone.",
                          trigger="Quarter panel replacement")
            if "fuel tank" not in ops_text:
                self._add("Repair Procedures", "R&I Fuel Tank", 1.5, self.mech_rate,
                          "Required for access and safety when welding or sectioning the rear quarter / rocker area. The fuel tank must be removed or protected per OEM procedures before any welding in the area.",
                          trigger="Quarter panel replacement")
            if "road test" not in ops_text:
                self._add("Repair Procedures", "Road Test", 0.5, self.body_rate,
                          "Required after structural repairs and ADAS calibrations to verify proper operation of all systems before returning the vehicle to the customer.",
                          trigger="Structural repair / ADAS calibration")
            if "welding blanket" not in ops_text and "weld blanket" not in ops_text:
                self._add("Miscellaneous", "Welding Blanket — Prevent Interior Damage", 0.4, self.body_rate,
                          "Required to protect the interior from weld spatter and heat when sectioning or replacing the quarter panel.",
                          trigger="Quarter panel replacement")
                self.suggestions[-1]["amount"] = 43.00
            if "epoxy" not in ops_text and "sealer" not in ops_text:
                self._add("Miscellaneous", "Epoxy sealer over bare metal", 0.3, self.paint_rate,
                          "Required on bare metal after welding or panel replacement to restore corrosion protection before refinish.",
                          trigger="Quarter panel replacement")
            if "mask jamb" not in ops_text and "jamb" not in ops_text:
                self._add("Miscellaneous", "Mask jambs / openings", 0.5, self.body_rate,
                          "Required to protect door and liftgate openings from overspray when refinishing the quarter panel.",
                          trigger="Quarter panel refinish")
            if "destructive" not in ops_text and "test weld" not in ops_text:
                self._add("Repair Procedures", "Destructive Weld Tests", 0.3, self.body_rate,
                          "Required to verify welder settings and material before welding structural or cosmetic panels. CCC and OEM procedures list destructive weld testing as not included.",
                          trigger="Welding operation")
            if "aluminum" in ops_text or "alu" in ops_text:
                if "aluminum setup" not in ops_text and "dedicated tools" not in ops_text:
                    self._add("Repair Procedures", "Aluminum Repair Setup (Isolated Area & Dedicated Tools)", 1.0, self.body_rate,
                              "Required when repairing aluminum components (liftgate on this vehicle). Aluminum repair requires a dedicated isolated area and dedicated tools to prevent contamination.",
                              trigger="Aluminum component repair")
            if "chmsl" not in ops_text and "high-mount" not in ops_text and "stop lamp" not in ops_text:
                self._add("Repair Procedures", "R&I High-Mount Stop Lamp (CHMSL)", 0.2, self.body_rate,
                          "Required for access when repairing or refinishing the liftgate.",
                          trigger="Liftgate repair")
            if "lift cylinder" not in ops_text and "liftgate cylinder" not in ops_text:
                self._add("Repair Procedures", "R&I Liftgate Lift Cylinders (both)", 0.4, self.body_rate,
                          "Required when removing the liftgate or for access during liftgate repair. Follow OEM one-time-use fastener requirements.",
                          trigger="Liftgate repair")

            # GM Quarter Outer Panel Sectioning — interior access items from RepairLogic
            if "sunshade" not in ops_text:
                self._add("Repair Procedures", "R&I Sunshade", 0.3, self.body_rate,
                          "Required for access when removing the headliner on a quarter panel sectioning. GM Headlining Trim Panel Replacement procedure requires sunshade removal.",
                          trigger="Quarter panel replacement / sectioning")
            if "assist handle" not in ops_text and "grab handle" not in ops_text:
                self._add("Repair Procedures", "R&I Roof Rail Assist Handles", 0.4, self.body_rate,
                          "Required for access when removing the headliner. GM Roof Rail Assist Handle Replacement is part of the headliner R&I for quarter panel sectioning.",
                          trigger="Quarter panel replacement / sectioning")
            if "garnish" not in ops_text and "windshield garnish" not in ops_text:
                self._add("Repair Procedures", "R&I Windshield Garnish Moldings", 0.4, self.body_rate,
                          "Required for access when removing the headliner. GM Windshield Garnish Molding Replacement is part of the headliner R&I procedure.",
                          trigger="Quarter panel replacement / sectioning")
            if "roof wiring" not in ops_text and "roof harness" not in ops_text:
                self._add("Repair Procedures", "R&I Roof Wiring Harness", 0.6, self.body_rate,
                          "Required for access when sectioning the quarter panel. The roof wiring harness runs along the roof rail and must be protected or removed during the weld zone work.",
                          trigger="Quarter panel replacement / sectioning")
            if "video display" not in ops_text and "rear seat entertainment" not in ops_text:
                self._add("Repair Procedures", "R&I Video Display (Second / Third Row)", 0.4, self.body_rate,
                          "Required for access when removing the headliner on vehicles equipped with rear seat entertainment. GM Video Display Replacement is part of the headliner R&I.",
                          trigger="Quarter panel replacement / sectioning")
            if "sir" not in ops_text and "disable" not in ops_text:
                self._add("Repair Procedures", "SIR Disabling and Enabling", 0.5, self.mech_rate,
                          "Required before any work on or near the SRS / curtain air bag. GM SIR Disabling and Enabling procedure must be followed to prevent accidental deployment.",
                          trigger="Quarter panel replacement / sectioning")
            if "courtesy lamp" not in ops_text and "reading lamp" not in ops_text:
                self._add("Repair Procedures", "R&I Rear Seat Position Center Reading and Courtesy Lamp", 0.2, self.body_rate,
                          "Required for access when removing the headliner. GM procedure is part of the headlining trim panel replacement for quarter panel sectioning.",
                          trigger="Quarter panel replacement / sectioning")

        # De-nib & finesse / finish sand & buff (not included in refinish time)
        if any(x in ops_text for x in ["refn", "refinish", "blend", "blnd", "clear coat"]) and "de-nib" not in ops_text and "finesse" not in ops_text and "buff" not in ops_text:
            self._add("Repair Procedures", "De-nib & finesse / finish sand & buff", 0.4, self.paint_rate,
                      "Not included in published refinish times. Needed to remove dust nibs and match the texture of the surrounding paint. CCC and MOTOR P-pages list de-nib / finish sand & buff as not included in the base refinish allowance.")

        # Rule: Adhesion promoter for flexible plastic parts
        plastic_parts = ["bumper", "grille", "mirror", "spoiler", "rocker", "molding", "fascia", "cover", "cladding"]
        if any(x in ops_text for x in plastic_parts) and any(x in ops_text for x in ["refn", "refinish", "paint", "blend", "blnd"]):
            if "adhesion promoter" not in ops_text and "promoter" not in ops_text:
                self._add("Miscellaneous", "Adhesion promoter (flexible plastic)", 0.3, self.paint_rate,
                          "Bumpers, cladding, and mirror caps are flexible plastic that paint won't grip on its own. An adhesion promoter is applied first so the finish bonds to the plastic and doesn't flake off the first time the part flexes. CCC lists this as a separate, billable operation (Adhesion promoter (unprimed flexible component) — Not Included).")
        if any(x in ops_text for x in ["bumper", "fascia", "grille", "spoiler"]) and "repl" in ops_text:
            if "raw" not in ops_text and "substrate" not in ops_text and "adhesion promoter" not in ops_text:
                self._add("Miscellaneous", "Raw substrate prep (unprimed plastic)", 0.2, self.paint_rate,
                          "Not included for new unprimed plastic parts. The surface must be cleaned and treated with adhesion promoter so the paint sticks. CCC and MOTOR list raw substrate prep as a not-included operation on unprimed plastics.")

        # Sound deadening (not included on many panel R&R)
        if any(x in ops_text for x in ["door shell", "door panel", "quarter", "hood", "trunk"]) and "sound" not in ops_text and "deadening" not in ops_text:
            self._add("Miscellaneous", "Replace sound deadening", 0.3, self.body_rate,
                      "Not included in most panel R&R times per Mitchell/MOTOR P-pages.")

        # Mask interior / overspray (not included)
        if any(x in ops_text for x in ["refn", "refinish", "blend", "blnd"]) and "mask interior" not in ops_text and "overspray" not in ops_text:
            self._add("Miscellaneous", "Cover / mask interior (overspray protection)", 0.3, self.body_rate,
                      "Once a door or opening is exposed, paint overspray drifts straight into the cabin and settles on seats, the dash, and the headliner. Covering the interior keeps that mist off surfaces you can't easily clean, so the customer doesn't get the car back with a film inside. CCC lists this as a separate, billable operation (Cover / mask interior of vehicle to prevent overspray damage — Not Included).")
        has_repair = any(x in ops_text for x in ["rpr", "repair", "blnd", "blend"])
        has_paint = any(x in ops_text for x in ["refn", "refinish", "paint", "blend", "blnd", "clear coat"])
        if has_repair and has_paint and "primer" not in ops_text:
            self._add("Miscellaneous", "Primer (repaired panels — 3M)", 0.0, 0.0,
                      "Required on repaired panels before refinish. 3M primer/sealer typical retail ~$20–$28.")
            self.suggestions[-1]["amount"] = 24.00
            self.suggestions[-1]["description"] += " — est. $24"
        paint_booth = any(x in ops_text for x in ["refn", "refinish", "blend", "clear coat", "paint", "basecoat", "primer"])
        if paint_booth and "car cover" not in ops_text and "vehicle cover" not in ops_text:
            self._add("Miscellaneous", "Car cover (paint booth)", 0.2, self.body_rate,
                      "Required to protect the vehicle while it is in the paint booth.")
        # Rule: Reset TPMS after wheel or tire work
        if any(x in ops_text for x in ["wheel", "tire", "rim", "valve stem", "tpms"]):
            if "tpms" not in ops_text and "reset" not in ops_text and "relearn" not in ops_text:
                self._add("Repair Procedures", "Reset TPMS", 0.5, self.mech_rate,
                          "Once the wheels or tires have been worked, the tire-pressure sensors have to be relearned or the warning light stays on. This is the relearn that clears that light and gets the system reading each wheel correctly again. CCC lists this as a separate, billable operation (Scan tool clear / reset — Not Included).")
        why_parts = []
        
        if any(x in ops_text for x in ["wheel", "tire", "rim", "suspension", "control arm", "strut", "knuckle"]):
            needs_alignment = True
            why_parts.append("wheel/tire or suspension work")
        
        if any(x in ops_text for x in ["rail", "apron", "pillar", "rocker", "section", "structural", "frame", "unibody"]):
            needs_alignment = True
            why_parts.append("structural repairs")
        
        # Static calibrations often require alignment first or after
        if any(s["description"].lower().startswith("static") for s in self.suggestions):
            needs_alignment = True
            why_parts.append("static ADAS calibration")
        
        if needs_alignment and "alignment" not in ops_text and "align" not in ops_text:
            why = "Required after " + ", ".join(why_parts) + " to restore proper geometry and support accurate ADAS calibration."
            self._add("Repair Procedures", "Four-wheel alignment", 1.2, self.mech_rate, why)
        if any(x in ops_text for x in ["rpr bumper", "repair bumper", "bumper rpr", "bumper cover"]) and "kit" not in ops_text:
            if "refn" in ops_text or "refinish" in ops_text or "paint" in ops_text or "blend" in ops_text:
                self._add("Parts Differences", "Bumper repair kit (3M materials)", 0.0, 0.0,
                          "Includes 3M adhesives, fillers, and materials required to properly repair a bumper cover. Typical retail ~$45–$55.")
                self.suggestions[-1]["amount"] = 50.00
                self.suggestions[-1]["description"] += " — est. $50"
        front_rear_hit = any(x in ops_text for x in [
            "bumper", "grille", "radiator", "condenser", "core support", 
            "apron", "rail", "trunk", "tail lamp", "taillamp", "liftgate", "quarter"
        ])
        if front_rear_hit and "measure" not in ops_text and "structural" not in ops_text and "frame" not in ops_text:
            self._add("Repair Procedures", "Setup and Measure", 1.0, self.body_rate,
                      "A collision can push the body structure out of its factory dimensions, so the vehicle is set up and measured before the repair plan is final. Structural damage diagnosis and vehicle set up time is listed in CCC's Labor Time Premise as not included in any estimated work time.",
                      trigger="Direct front or rear impact")
        plastic_parts = ["bumper", "grille", "mirror", "spoiler", "rocker", "molding", "fascia", "cover"]
        paint_ops = any(x in ops_text for x in ["refn", "refinish", "paint", "blend"])
        if paint_ops and any(x in ops_text for x in plastic_parts):
            if "flex" not in ops_text and "additive" not in ops_text:
                self._add("Miscellaneous", "Flex additive (plastic parts)", 0.0, 0.0,
                          "Required when refinishing flexible plastic parts (bumpers, grilles, mirrors, etc.) so paint flexes without cracking. Typical material cost $8–$15.")
                self.suggestions[-1]["amount"] = 12.00
                self.suggestions[-1]["description"] += " — est. $12"
        if "hazardous" not in ops_text and "waste" not in ops_text and "disposal" not in ops_text:
            self._add("Miscellaneous", "Hazardous waste disposal", 0.0, 0.0,
                      "Required for disposal of paint waste, solvents, old parts, and other regulated materials. Typical flat fee $15–$35.")
            self.suggestions[-1]["amount"] = 25.00
            self.suggestions[-1]["description"] += " — est. $25"
        electrical_parts = ["headlamp", "head lamp", "taillamp", "tail lamp", "mirror", "module", 
                           "sensor", "radar", "camera", "srs", "air bag", "restraint", "abs", "ecm"]
        if any(x in ops_text for x in electrical_parts):
            if "mask" not in ops_text and "connector" not in ops_text and "protect connector" not in ops_text:
                self._add("Miscellaneous", "Mask electrical connectors / wiring harness", 0.3, self.body_rate,
                          "Paint and primer build up on connectors and harnesses cause poor contact and corrosion down the road. Masking the wiring before refinish keeps the electrical components clean so nothing gets intermittent later. CCC notes that estimated work time does not include capping or covering electrical connectors and fluid lines to prevent contamination.")
        if "pre-clean" not in ops_text and "preclean" not in ops_text and "pre clean" not in ops_text:
            self._add("Miscellaneous", "Pre-clean vehicle", 0.5, self.body_rate,
                      "Required before repairs to remove dirt, grease, and road film so materials and paint adhere properly.",
                      trigger="Any collision repair")
        if any(x in ops_text for x in ["weld", "section", "rail", "apron", "pillar", "rocker", "quarter"]):
            if "welder set" not in ops_text and "weld set" not in ops_text and "setup" not in ops_text:
                self._add("Repair Procedures", "Welder set-up", 0.5, self.body_rate,
                          "Required before any welding operation per estimating systems and OEM procedures.")
            if "test weld" not in ops_text and "testweld" not in ops_text:
                self._add("Repair Procedures", "Test weld", 0.3, self.body_rate,
                          "Required to verify welder settings and material before welding structural or cosmetic panels.")
            if "weld burn" not in ops_text and "weldburn" not in ops_text and "burn rpr" not in ops_text:
                self._add("Repair Procedures", "Weld burn Rpr/Refn (adjacent panels)", 0.5, self.body_rate,
                          "Adjacent panels often require repair and refinish from weld heat/burn. Commonly missed on sectioning or panel replacement.")
        if "calibrat" not in ops_text and "adas" not in ops_text:
            # Front camera (windshield or forward-facing camera work)
            if any(x in ops_text for x in ["windshield", "cowl", "camera"]):
                if self._has_option(["camera", "lane", "forward collision", "adas"]):
                    self._add("Pre/Post Scans", "Static Forward Facing Camera Calibration", 0.0, 0.0,
                              "Honda requires software-based aiming of the forward camera (ACC, CMBS, LKAS, LDW, etc.) after windshield R&R or camera R&I. Systems often will not display DTCs unless damaged, so aiming may still be required. Four-wheel alignment should be verified before aiming. Only i-HDS is approved. Typical invoice $550.",
                              trigger="Windshield / camera work")
                    self.suggestions[-1]["amount"] = 550.00
                    self.suggestions[-1]["description"] += " — est. $550"

            # Front radar / ACC (bumper, grille, front-end) — only if equipped or no build sheet
            if any(x in ops_text for x in ["bumper", "grille", "radar", "acc"]):
                if self._has_option(["radar", "acc", "adaptive cruise", "intelligent cruise"]):
                    self._add("Pre/Post Scans", "Static Forward Radar Calibration", 0.0, 0.0,
                              "Kia requires front radar alignment after collision or bumper work that may have disturbed the radar (check for bent brackets or bumper beam). Honda requires millimeter wave radar aiming after radar R&I or front bumper fascia repair within 300 mm of the radar. Four-wheel alignment should be verified before aiming. Typical invoice $495.",
                              trigger="Bumper / radar work")
                self.suggestions[-1]["amount"] = 495.00
                self.suggestions[-1]["description"] += " — est. $495"

                # Many platforms also need dynamic or dual
                if self._has_option(["radar", "acc", "adaptive cruise", "intelligent cruise"]):
                    self._add("Pre/Post Scans", "Dynamic Forward Radar Calibration", 0.0, 0.0,
                              "Often required after static or as the primary method on some platforms. Typical invoice $375.")
                    self.suggestions[-1]["amount"] = 375.00
                    self.suggestions[-1]["description"] += " — est. $375"

            # Steering angle (alignment or front-end work)
            if any(x in ops_text for x in ["alignment", "steer", "suspension", "control arm"]):
                self._add("Pre/Post Scans", "Steering Angle Sensor Calibration", 0.0, 0.0,
                          "Required after alignment or certain front-end/suspension work on most modern vehicles. Typical invoice $100.")
                self.suggestions[-1]["amount"] = 100.00
                self.suggestions[-1]["description"] += " — est. $100"

            # SRS / seat occupancy
            if any(x in ops_text for x in ["srs", "air bag", "restraint", "seat", "occupant"]):
                self._add("Pre/Post Scans", "Passenger Seat ODS / Zero-Point Calibration", 0.0, 0.0,
                          "Required after SRS component service or seat work. Typical invoice $100.")
                self.suggestions[-1]["amount"] = 100.00
                self.suggestions[-1]["description"] += " — est. $100"

            # Blind spot / side mirrors
            if any(x in ops_text for x in ["mirror", "quarter", "door", "blind"]):
                if self._has_option(["blind spot", "blind-spot", "cross path"]):
                    self._add("Pre/Post Scans", "Blind Spot Assist System Calibration (Static)", 0.0, 0.0,
                              "Required after mirror or rear quarter work if equipped. Typical invoice $495.")
                    self.suggestions[-1]["amount"] = 495.00
                    self.suggestions[-1]["description"] += " — est. $495"

            # Rear / park assist
            if any(x in ops_text for x in ["rear bumper", "tail lamp", "park assist", "sonar"]):
                if self._has_option(["parking sensor", "park assist", "sonar", "park sense"]):
                    self._add("Pre/Post Scans", "Park Assist / Sonar Reset or Adjust", 0.0, 0.0,
                              "Common after rear bumper work. Typical invoice $350.")
                    self.suggestions[-1]["amount"] = 350.00
                    self.suggestions[-1]["description"] += " — est. $350"
        
        return self.suggestions
    
    def _add(self, category: str, description: str, hours: float, rate: float = None, why: str = "", trigger: str = ""):
        if rate is None:
            rate = self.body_rate
        if isinstance(rate, str):
            rate = float(rate)
        amount = hours * rate
        self.suggestions.append({
            "category": category,
            "description": description,
            "hours": hours,
            "rate": rate,
            "amount": round(amount, 2),
            "why": why,
            "trigger": trigger,
            "selected": True
        })


def analyze_estimate(pdf_path: str, build_sheet_path: Optional[str] = None) -> Dict:
    parser = EstimateParser(pdf_path)
    data = parser.parse()
    build_sheet = None
    if build_sheet_path:
        try:
            build_sheet = parse_build_sheet(build_sheet_path)
        except Exception:
            build_sheet = None
    analyzer = MissedOpsAnalyzer(data, build_sheet)
    suggestions = analyzer.analyze()
    
    total_missed = sum(s["amount"] for s in suggestions if s["selected"])
    
    return {
        "estimate": data,
        "build_sheet": build_sheet,
        "suggestions": suggestions,
        "total_potential": total_missed,
        "suggestion_count": len(suggestions)
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = analyze_estimate(sys.argv[1])
        print(json.dumps(result, indent=2))
    else:
        print("Usage: python estimate_analyzer.py <pdf_path>")
