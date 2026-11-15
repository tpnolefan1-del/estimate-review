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
        m = re.search(r"VEHICLE\s*\n\s*(20\d{2}\s+\S+(?:\s+\S+){0,6})", text)
        if m:
            raw = m.group(1).strip()
            # Expand truncated makes
            raw = re.sub(r"\bNISS\b", "NISSAN", raw, flags=re.I)
            raw = re.sub(r"\bCHEV\b", "CHEVROLET", raw, flags=re.I)
            raw = re.sub(r"\bTOYO\b", "TOYOTA", raw, flags=re.I)
            raw = re.sub(r"\bHOND\b", "HONDA", raw, flags=re.I)
            # Take year + make + model + trim (first 4 tokens)
            parts = raw.split()
            ymm_parts = []
            for i, p in enumerate(parts[:4]):
                if p.isdigit():
                    ymm_parts.append(p)
                elif i == 3 and len(p) <= 3:  # trim code like SL, LTZ
                    ymm_parts.append(p.upper())
                else:
                    ymm_parts.append(p.title())
            self.data["year_make_model"] = " ".join(ymm_parts)
        
        # Exterior color
        m = re.search(r"Exterior Color:\s*([A-Za-z\s/]+?)(?:\s+Mileage|\s+VIN|\n|$)", text)
        if m:
            self.data["exterior_color"] = m.group(1).strip()
        else:
            self.data["exterior_color"] = ""
        
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
                          "CCC lists R&I of adjacent components for access as not included in the bumper cover R&I time. The upper tabs of the bumper cover sit behind the headlamps; the lamps must be removed to service the cover. Cited in CCC MOTOR Guide to Estimating — Labor Time Premise.",
                          trigger="Bumper cover R&I")
            if "energy absorber" not in ops_text and "impact bar" not in ops_text:
                self._add("Remove & Install", "R&I Front energy absorber / impact bar", 0.4,
                          why="Often required when bumper cover is replaced; check if present on vehicle.",
                          trigger="Bumper cover R&I")
        
        # Rule 2: Scans / Diagnostics — use realistic sublet pricing
        if "scan" not in ops_text and "diagnostic" not in ops_text:
            self._add("Pre/Post Scans", "Pre-repair diagnostic scan", 0.0, 0.0,
                      "Nissan (Nov 2025) requires pre- and post-repair scans on all 2008+ vehicles using the CONSULT diagnostic tool only. Honda (July 2025), Ford (Oct 2025), Kia, Toyota CRIB #191, FCA, and GM also require pre-repair scans. Not all DTCs illuminate a warning light. A full scan (OEM-grade scan tool, DTC read/clear, and documentation) is required. Cited in OEM position statements and CCC MOTOR Guide to Estimating — Labor Time Premise (Diagnostic scanning is not included).",
                      trigger="Any collision repair")
            self.suggestions[-1]["amount"] = 73.50  # P-Page code 12
            self.suggestions[-1]["description"] += " — $73.50"
            self.suggestions[-1]["why"] = "Nissan (Nov 2025) requires pre- and post-repair scans on all 2008+ vehicles using the CONSULT diagnostic tool only. Honda (July 2025), Ford (Oct 2025), Kia, Toyota CRIB #191, FCA, and GM also require pre/post scans. Not all DTCs illuminate a warning light. Typical invoice $50–$130."
            self.suggestions[-1]["trigger"] = "Any collision repair"
            
            self._add("Pre/Post Scans", "Post-repair diagnostic scan", 0.0, 0.0,
                      "Toyota CRIB #191, FCA, and GM require a post-repair diagnostic scan to confirm all systems are functioning properly and to identify any systems that need repair, reprogramming, or initialization. GM requires retesting after all repairs are complete to verify faults have been repaired and no new faults were introduced. Typical invoice $50–$130.",
                      trigger="Any collision repair")
            self.suggestions[-1]["amount"] = 128.95  # P-Page code Post
            self.suggestions[-1]["description"] += " — $128.95"
        
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
            ymm = (self.data.get("year_make_model") or "").upper()
            # Only fire for OEMs that have a published post-collision seat belt inspection statement
            oem_statements = {
                "GM": ["CHEVROLET", "GMC", "BUICK", "CADILLAC"],
                "FCA": ["CHRYSLER", "DODGE", "JEEP", "RAM"],
                "TOYOTA": ["TOYOTA", "LEXUS", "SCION"],
            }
            matched_oem = None
            for oem, makes in oem_statements.items():
                if any(mk in ymm for mk in makes):
                    matched_oem = oem
                    break
            if matched_oem:
                self._add("Repair Procedures", "Post-collision seat belt inspection", 0.5, self.mech_rate,
                          f"{matched_oem} requires inspection of all seat belts after any collision before returning the vehicle to the customer. Real-world inspections have uncovered deformed air bags and tweaked mounting brackets with no warning lights or DTCs. {matched_oem} requires replacement of any seat belt system in use during a deployment event or if components are torn, worn, or damaged. Cited in {matched_oem} collision repair position statements.",
                          trigger=f"{matched_oem} vehicle — any collision repair")
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
        
        # OEM Parts Conversion — flag aftermarket parts when OEM prohibits them
        ymm = (self.data.get("year_make_model") or "").upper()
        # OEMs with explicit no-aftermarket / no-salvage position statements (from oem1stop.com)
        # Exact language + PDF link for each
        oem_statements = {
            "GM": {
                "makes": ["CHEVROLET", "GMC", "BUICK", "CADILLAC"],
                "quote": "GM does not approve or support the use of salvage, recycled/used, or non-Genuine aftermarket parts on Chevrolet, Buick, GMC, and Cadillac vehicles.",
                "link": "https://oem1stop.com/"
            },
            "Mazda": {
                "makes": ["MAZDA"],
                "quote": "Mazda North American Operations does not authorize the use of the following parts: aftermarket, third-party replacement parts, gray market, imitation parts, used parts, damaged fasteners and hardware, salvaged/recycled parts and components, reconditioned parts.",
                "link": "http://oem1stop.com/wp-content/uploads/2026/10/Mazda_Parts_Position_Statement12-6-24.pdf"
            },
            "Nissan": {
                "makes": ["NISSAN", "INFINITI"],
                "quote": "Nissan North America DOES NOT approve of the use of aftermarket, gray market, or imitation parts. Nissan North America DOES NOT approve of the repair of our vehicles with salvage parts or assemblies.",
                "link": "http://oem1stop.com/wp-content/uploads/2026/10/Nissan_Pos_AM-Parts-Usage_20168-7-20.pdf"
            },
            "Volkswagen": {
                "makes": ["VOLKSWAGEN", "VW", "AUDI"],
                "quote": "VWoA does not support the repair of Volkswagen vehicles with recycled vehicle parts or assemblies. Recycled, salvaged, aftermarket, and reconditioned parts (including body parts, wheels, and safety restraint components) are not authorized by VWoA.",
                "link": "https://oem1stop.com/wp-content/uploads/2026/10/VW_Pos_Recycled-Parts_20263-5-26.pdf"
            },
            "Lincoln": {
                "makes": ["LINCOLN"],
                "quote": "The Lincoln Motor Company does not approve the use of recycled, salvaged, aftermarket or reconditioned parts. Recycled, salvaged, aftermarket and reconditioned parts (including body parts, wheels and safety restraint components) are not authorized by The Lincoln Motor Company.",
                "link": "https://www.oem1stop.com/sites/default/files/Lincoln_Use_of_NonOEM_Parts%20-%20Position%20Statement%20_FNL-2022.pdf"
            },
            "Mercedes-Benz": {
                "makes": ["MERCEDES", "MERCEDES-BENZ"],
                "quote": "Mercedes-Benz does not approve of the use of aftermarket or third-party replacement parts. Mercedes-Benz does not approve of the use of components removed from a vehicle that was damaged, burned, flooded, scrapped, or involved in a previous collision.",
                "link": "http://oem1stop.com/wp-content/uploads/2026/10/MBUSA-Position-Statement-for-Genuine-Replacement-Parts.pdf"
            },
            "Genesis": {
                "makes": ["GENESIS"],
                "quote": "GENESIS MOTOR AMERICA DOES NOT RECOMMEND THE USE OF COUNTERFEIT, GREY MARKET, AFTERMARKET, IMITATION, RECYCLED COLLISION REPAIR PARTS, ALTERNATE OR OTHER NON-ORIGINAL EQUIPMENT PARTS.",
                "link": "https://oem1stop.com/sites/default/files/Genesis_Pos_Non-OEM%20Parts%284-23%29.pdf"
            },
            "Rivian": {
                "makes": ["RIVIAN"],
                "quote": "Rivian does not recommend the use of aftermarket parts on any Rivian vehicle. Rivian does not support the use of aftermarket, Optional OEM (Opt-OE), alternative, reverse-engineered, used, salvaged, or externally re-manufactured parts. Rivian does not recommend the use of any salvaged, used, or recycled part that is a component of the Driver+ system (ADAS).",
                "link": "https://oem1stop.com/sites/default/files/Rivian_Pos_Repair-Standards%284-28-26%29.pdf"
            },
        }
        matched_oem = None
        matched_info = None
        for oem, info in oem_statements.items():
            if any(mk in ymm for mk in info["makes"]):
                matched_oem = oem
                matched_info = info
                break
        if matched_oem and matched_info:
            am_parts = []
            for item in self.data.get("line_items", []):
                desc = (item.get("description") or "").lower()
                if any(x in desc for x in ["a/m", "aftermarket", "lkq", "salvage", "recycled", "used"]):
                    am_parts.append(item.get("description", "")[:60])
            if am_parts:
                parts_list = "; ".join(am_parts[:5])
                self._add("OEM Compliance", "OEM Parts Conversion", 0.0, 0.0,
                          f"{matched_info['quote']} Parts flagged: {parts_list}. Convert each to the OEM part and bill the difference. Position statement: {matched_info['link']}",
                          trigger=f"{matched_oem} vehicle with aftermarket parts")
                self.suggestions[-1]["amount"] = 0.00
        
        # LKQ / used parts when OEM requires OEM
        if matched_oem:
            lkq_parts = []
            for item in self.data.get("line_items", []):
                desc = (item.get("description") or "").lower()
                if any(x in desc for x in ["lkq", "salvage", "recycled", "used", "rcy"]):
                    lkq_parts.append(item.get("description", "")[:60])
            if lkq_parts:
                parts_list = "; ".join(lkq_parts[:5])
                self._add("OEM Compliance", "LKQ / used parts not approved", 0.0, 0.0,
                          f"{matched_info['quote']} Parts flagged: {parts_list}. Replace with genuine OEM parts. Position statement: {matched_info['link']}",
                          trigger=f"{matched_oem} vehicle with LKQ / used parts")
                self.suggestions[-1]["amount"] = 0.00
        
        # Structural parts must be OEM
        structural_keywords = ["rail", "apron", "pillar", "rocker", "frame", "unibody", "reinforcement", "crossmember"]
        has_structural = any(any(kw in (item.get("description") or "").lower() for kw in structural_keywords) for item in self.data.get("line_items", []))
        if has_structural and matched_oem:
            non_oem_struct = []
            for item in self.data.get("line_items", []):
                desc = (item.get("description") or "").lower()
                if any(kw in desc for kw in structural_keywords) and any(x in desc for x in ["a/m", "aftermarket", "lkq", "salvage"]):
                    non_oem_struct.append(item.get("description", "")[:60])
            if non_oem_struct:
                parts_list = "; ".join(non_oem_struct[:5])
                self._add("OEM Compliance", "Structural parts must be OEM", 0.0, 0.0,
                          f"{matched_oem} requires genuine OEM parts for structural components. Non-OEM structural parts flagged: {parts_list}. Structural repairs performed with non-OEM parts may not restore the vehicle to pre-accident condition. Quoted from {matched_oem} collision repair position statements.",
                          trigger=f"{matched_oem} vehicle with non-OEM structural parts")
                self.suggestions[-1]["amount"] = 0.00
        
        # ADAS bumper — aftermarket not approved when ADAS shines through or is mounted in bumper
        if matched_oem and any(x in ops_text for x in ["bumper", "fascia"]):
            has_adas = self._has_option(["radar", "acc", "adas", "blind", "park assist", "camera"]) or any(x in ops_text for x in ["radar", "camera", "blind", "park assist", "adas"])
            if has_adas:
                am_bumper = any(any(x in (item.get("description") or "").lower() for x in ["a/m", "aftermarket", "lkq"]) and any(x in (item.get("description") or "").lower() for x in ["bumper", "fascia"]) for item in self.data.get("line_items", []))
                if am_bumper:
                    self._add("OEM Compliance", "ADAS bumper must be OEM", 0.0, 0.0,
                              f"{matched_oem} does not approve aftermarket, reconditioned, or salvage bumpers/fascias on ADAS-equipped vehicles. ADAS components (blind spot, radar, cameras) may be mounted in or shine through the bumper. Only genuine OEM bumper assemblies are approved. Quoted from {matched_oem} ADAS position statements.",
                              trigger=f"{matched_oem} ADAS vehicle with aftermarket bumper")
                    self.suggestions[-1]["amount"] = 0.00
        
        if ("door" in ops_text and "repl" in ops_text) or ("door" in ops_text and any(x in ops_text for x in ["detrim", "r&i", "remove", "refn", "refinish"])):
            if "watershield" not in ops_text and "water shield" not in ops_text and "butyl" not in ops_text:
                self._add("Miscellaneous", "Door Watershield Sealant", 0.0, 0.0,
                          "The watershield is bedded in a strip of butyl sealant that tears when the sheet is peeled off, so it needs new sealant to seal the door again.",
                          trigger="Door shell replacement or door detrim/removal for refinish")
                self.suggestions[-1]["amount"] = 24.00
                self.suggestions[-1]["description"] += " — est. $24"
        
        # Front door replacement → blend fender + blend rear door + related R&I
        front_door_repl = False
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if "front door" in line_text and any(x in line_text for x in ["repl", "section"]):
                front_door_repl = True
                break
        if front_door_repl:
            # Blend fender
            if "fender" not in ops_text or "blnd" not in ops_text:
                self._add("Repair Procedures", "Blend Fender (front door replacement)", 1.0, self.paint_rate,
                          "CCC lists blending into adjacent panels as not included in the panel's standard labor allowance. Fresh paint on the front door rarely matches the aged factory finish on the fender beside it, so the color is blended into the fender and the whole panel is cleared to make the change between them invisible. Cited in CCC MOTOR Guide to Estimating — Basic Color Coat Application: 'Does not include: Blending into adjacent panels.'",
                          trigger="Front door replacement")
            # Blend rear door
            if "rear door" not in ops_text or "blnd" not in ops_text:
                self._add("Repair Procedures", "Blend Rear Door (front door replacement)", 1.0, self.paint_rate,
                          "Fresh paint on the front door rarely matches the aged factory finish on the rear door beside it. Blend the rear door and clear the entire panel to make the color transition invisible.",
                          trigger="Front door replacement")
            # Fender blend access
            if "headlamp" not in ops_text and "head lamp" not in ops_text:
                self._add("Remove & Install", "R&I Headlamp (fender blend access)", 0.4, self.body_rate,
                          "Required for access to blend the fender and to protect the lamp from overspray.",
                          trigger="Blend fender")
            if "bumper cover" not in ops_text and "front bumper" not in ops_text:
                self._add("Remove & Install", "R&I Front Bumper Cover (fender blend access)", 0.6, self.body_rate,
                          "Required for access to blend the fender and to protect the bumper from overspray.",
                          trigger="Blend fender")
            if "fender liner" not in ops_text:
                self._add("Remove & Install", "R&I Fender Liner (fender blend access)", 0.3, self.body_rate,
                          "Required for access to blend the fender and to protect the liner from overspray.",
                          trigger="Blend fender")
            if "rocker molding" not in ops_text and "rocker moulding" not in ops_text:
                self._add("Remove & Install", "R&I Rocker Molding (if equipped)", 0.2, self.body_rate,
                          "Required for access to blend the fender if the vehicle is equipped with a rocker molding.",
                          trigger="Blend fender")
            if "mud guard" not in ops_text and "mudguard" not in ops_text:
                self._add("Remove & Install", "R&I Mud Guard (if equipped)", 0.2, self.body_rate,
                          "Required for access to blend the fender if the vehicle is equipped with a mud guard.",
                          trigger="Blend fender")
            if "upper molding" not in ops_text and "fender molding" not in ops_text:
                self._add("Remove & Install", "R&I Upper Molding for Fender (if equipped)", 0.2, self.body_rate,
                          "Required for access to blend the fender if the vehicle is equipped with an upper fender molding.",
                          trigger="Blend fender")
            # Rear door blend access
            if "belt molding" not in ops_text and "belt moulding" not in ops_text:
                self._add("Remove & Install", "R&I Rear Door Belt Molding", 0.3, self.body_rate,
                          "Required to detrim the rear door for blending. The belt molding must be removed to access the blend area and protect it from overspray.",
                          trigger="Blend rear door")
            if "outer handle" not in ops_text and "door handle" not in ops_text:
                self._add("Remove & Install", "R&I Rear Door Outer Handle", 0.3, self.body_rate,
                          "Required to detrim the rear door for blending. The outer handle must be removed to access the blend area and protect it from overspray.",
                          trigger="Blend rear door")
            if "trim panel" not in ops_text and "door panel" not in ops_text:
                self._add("Remove & Install", "R&I Rear Door Interior Trim Panel", 0.4, self.body_rate,
                          "Required to detrim the rear door for blending. The interior trim panel must be removed to access the blend area and protect it from overspray.",
                          trigger="Blend rear door")
        
        if "quarter" in ops_text and "repl" in ops_text:
            # Only trigger rolled hem flange verification if an LKQ / used quarter panel is on the estimate
            has_lkq_quarter = any("lkq" in (item.get("description","") + " " + item.get("operation","")).lower() and "quarter" in (item.get("description","") + " " + item.get("operation","")).lower() for item in self.data.get("line_items", []))
            if has_lkq_quarter and "hem flange" not in ops_text and "rolled hem" not in ops_text:
                self._add("Repair Procedures", "Rolled hem flange verification (quarter panel)", 0.0, 0,
                          "I-CAR notes that some vehicles have outer quarter panels with rolled hem flanges. Recycled (LKQ) outer quarter panels with rolled hem flanges require special consideration and may not be approved. Check the OEM Partial Part Replacement Search to confirm. Starting with 2015 model year vehicles, the note will state whether the vehicle has, does not have, or has not yet been identified for a rolled hem flange. Consider OEM instead of LKQ.",
                          trigger="LKQ / used quarter panel on estimate")
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
        
        # Heat repair on structural / HSS — FCA and Nissan prohibit
        if any(x in ops_text for x in ["heat", "heat repair", "hot", "oxy", "torch"]) and any(x in ops_text for x in ["structural", "rail", "apron", "pillar", "hss", "uhss"]):
            ymm = (self.data.get("year_make_model") or "").upper()
            if any(mk in ymm for mk in ["CHRYSLER", "DODGE", "JEEP", "RAM", "FIAT", "NISSAN"]):
                self._add("OEM Compliance", "Heat repair not approved on structural / HSS", 0.0, 0.0,
                          "FCA US LLC: If heat is used to straighten structural high-strength steels, advanced high-strength steel or ultra-high strength steel, the component must be replaced. Cold straightening may be used without replacement. Nissan: The repair of reinforcements (such as side members) by heating is not recommended since it may weaken the component. When heating is unavoidable, do not heat high-strength steel parts above 550°C (1022°F). Cited in FCA Use of Heat and Nissan Unibody/Structural Repair position statements on oem1stop.com.",
                          trigger="Heat repair on structural / HSS panel")
                self.suggestions[-1]["amount"] = 0.00

        # OEM glass — several OEMs require OEM glass (ADAS integration, structure)
        if any(x in ops_text for x in ["glass", "windshield", "wind shield"]) and any(x in ops_text for x in ["a/m", "aftermarket", "lkq", "salvage"]):
            ymm = (self.data.get("year_make_model") or "").upper()
            if any(mk in ymm for mk in ["NISSAN", "FORD", "MERCEDES", "CHRYSLER", "DODGE", "JEEP", "RAM", "RIVIAN"]):
                self._add("OEM Compliance", "OEM glass required", 0.0, 0.0,
                          "Nissan: DOES NOT support the use of aftermarket or recycled glass. Ford: does not endorse the use of non-Ford original equipment glass. Mercedes-Benz: Aftermarket variants do not meet the exacting specifications of genuine Mercedes-Benz glass. FCA: does not approve of or recognize glass replacement procedures where genuine Mopar Parts are not used. Glass integrates with ADAS cameras and provides structural rigidity. Cited in OEM glass position statements on oem1stop.com.",
                          trigger="Aftermarket / salvage glass on estimate")
                self.suggestions[-1]["amount"] = 0.00

        # Reconditioned wheels — FCA and Lincoln prohibit
        if any(x in ops_text for x in ["wheel", "rim"]) and any(x in ops_text for x in ["recond", "reconditioned", "refinish", "straighten"]):
            ymm = (self.data.get("year_make_model") or "").upper()
            if any(mk in ymm for mk in ["CHRYSLER", "DODGE", "JEEP", "RAM", "LINCOLN", "FORD", "RIVIAN"]):
                self._add("OEM Compliance", "Reconditioned wheels not approved", 0.0, 0.0,
                          "FCA US LLC does not recommend the use of reconditioned wheels (wheels that have been damaged and repaired) because they can result in a sudden catastrophic wheel failure. Lincoln: does not approve the remanufacturing/refinishing of steel or aluminum wheels when it involves re-machining, re-plating, welding, bending, straightening, or reforming. Rivian: does not support or approve the use of reconditioned (damaged and repaired) wheels. Reconditioning may include heating, straightening, welding, material removal, reshaping, or re-plating, which can affect the strength of the wheel. Cited in OEM position statements on oem1stop.com.",
                          trigger="Reconditioned / refinished wheel on estimate")
                self.suggestions[-1]["amount"] = 0.00

        # OEM-specific scan tool requirement
        ymm = (self.data.get("year_make_model") or "").upper()
        oem_scan_tools = {
            "Honda": {"makes": ["HONDA", "ACURA"], "tool": "i-HDS", "quote": "The use of the i-HDS software is the only way to perform a proper OEM Diagnostic Scan for Honda and Acura vehicles."},
            "Nissan": {"makes": ["NISSAN", "INFINITI"], "tool": "CONSULT", "quote": "Nissan (Nov 2025) requires pre- and post-repair scans on all 2008+ vehicles using the CONSULT diagnostic tool only."},
            "FCA": {"makes": ["CHRYSLER", "DODGE", "JEEP", "RAM", "FIAT"], "tool": "wiTECH", "quote": "Use of the Mopar wiTECH vehicle diagnostic tester (scan tool) is necessary before and after collision repair."},
            "Ford": {"makes": ["FORD", "LINCOLN"], "tool": "FDRS / IDS", "quote": "Vehicle diagnostic testing, including pre/post scanning, module programming and system calibrations is to be conducted using required Ford Motor Company diagnostic software (FDRS / IDS) and recommended hardware."},
            "Mazda": {"makes": ["MAZDA"], "tool": "Genuine Mazda scan tool", "quote": "Mazda requires pre- and post-repair scans using a genuine Mazda scan tool."},
            "Rivian": {"makes": ["RIVIAN"], "tool": "RiDE (Rivian Diagnostic Engine)", "quote": "A pre-repair and post-repair scan of the vehicle using the Rivian Diagnostic Engine (RiDE) is required. All scans and calibrations must be performed by Rivian Certified Technicians."},
        }
        for oem, info in oem_scan_tools.items():
            if any(mk in ymm for mk in info["makes"]):
                if "scan" not in ops_text or info["tool"].lower() not in ops_text:
                    self._add("Pre/Post Scans", f"OEM scan tool required ({info['tool']})", 0.0, 0.0,
                              f"{info['quote']} Aftermarket scan tools may not contain the software needed to properly assess all systems. Position statement available on oem1stop.com.",
                              trigger=f"{oem} vehicle — OEM scan tool required")
                    self.suggestions[-1]["amount"] = 0.00
                break

        # Liftgate / deck lid / trunk lid repair or replacement → align hinges and drop headliner
        # These terms are equivalent — only one will be present on any vehicle
        lid_work = False
        lid_name = "liftgate"
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if any(x in line_text for x in ["liftgate", "lift gate", "deck lid", "decklid", "trunk lid", "trunklid"]) and any(x in line_text for x in ["rpr", "repair", "repl", "replace", "r&i"]):
                lid_work = True
                if "deck" in line_text:
                    lid_name = "deck lid"
                elif "trunk" in line_text:
                    lid_name = "trunk lid"
                else:
                    lid_name = "liftgate"
                break
        if lid_work:
            if "align" not in ops_text or "hinge" not in ops_text:
                self._add("Miscellaneous", f"Align Right {lid_name.title()} Hinge", 0.2, self.body_rate,
                          f"A repaired or replaced {lid_name} can sit out of line, so it is adjusted at the right hinge to set even gaps to the body around it. The published flat {lid_name} time under-allows this fitment; it is separate body labor. CCC lists hinge alignment as not included in the {lid_name} R&I or repair time.",
                          trigger=f"{lid_name.title()} repair, replacement, or R&I")
                self._add("Miscellaneous", f"Align Left {lid_name.title()} Hinge", 0.2, self.body_rate,
                          f"A repaired or replaced {lid_name} can sit out of line, so it is adjusted at the left hinge to set even gaps to the body around it. The published flat {lid_name} time under-allows this fitment; it is separate body labor. CCC lists hinge alignment as not included in the {lid_name} R&I or repair time.",
                          trigger=f"{lid_name.title()} repair, replacement, or R&I")
            if "headliner" not in ops_text and lid_name == "liftgate":
                self._add("Remove & Install", f"Drop Headliner ({lid_name} electrical access)", 0.8, self.body_rate,
                          f"Required to access the {lid_name} electrical connector for removal and reinstall. The headliner must be dropped to disconnect the wiring harness at the {lid_name}. CCC lists R&I of adjacent components for access as not included in the {lid_name} R&I time.",
                          trigger=f"{lid_name.title()} repair, replacement, or R&I")
            if "chmsl" not in ops_text and "high-mount" not in ops_text and "stop lamp" not in ops_text and lid_name == "liftgate":
                self._add("Repair Procedures", "R&I High-Mount Stop Lamp (CHMSL)", 0.2, self.body_rate,
                          "Required for access when repairing or refinishing the liftgate. Only applies to true liftgates, not trunk lids or deck lids.",
                          trigger="Liftgate repair")

        # Rear body panel replacement — comprehensive access and materials
        rear_body_repl = False
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if any(x in line_text for x in ["rear body", "rear panel", "body panel"]) and "repl" in line_text:
                rear_body_repl = True
                break
        if rear_body_repl:
            # Access operations (hours from Tucson 3721 estimate)
            access_ops = [
                ("R&I Liftgate Opening Weatherstrip", 0.5, "Required to access the rear body panel and to reseal the opening after panel replacement."),
                ("R&I Rear Sill Trim", 0.2, "Required for access to the rear body panel and to protect the trim during welding."),
                ("R&I Rear Floor Covering", 0.1, "Required for access to the rear body panel and to protect the floor during welding."),
                ("R&I Spare Tire (if equipped)", 0.2, "Required for access to the rear body panel and to protect the spare during welding."),
                ("R&I Rear Cargo Box (if equipped)", 0.3, "Required for access to the rear body panel and to protect the cargo box during welding."),
                ("R&I Jack and Jack Storage (if equipped)", 0.2, "Required for access to the rear body panel and to protect the jack during welding."),
                ("R&I Exhaust (rearward of muffler)", 1.1, "Required for access to the rear body panel and to protect the exhaust during welding."),
                ("R&I Rear Seat", 0.5, "Required for access to the rear body panel and to protect the seat during welding."),
                ("R&I Lower Interior Quarter Trims", 0.4, "Required for access to the rear body panel and to protect the trim during welding."),
                ("R&I Rear Quarter Vent", 0.1, "Required for access to the rear body panel and to protect the vent during welding."),
                ("R&I Rear Fender Liner (weld zone)", 0.4, "Required to protect the liner from weld spatter and fire during welding of the rear body panel."),
                ("Disconnect Battery", 0.3, "Required before welding and electrical work to prevent system faults and accidental deployment."),
                ("D&R Wiring Harness", 0.5, "Required to disconnect the wiring harness for rear body panel replacement and to protect it during welding."),
                ("R&I Rear Blind Spot Radar (if equipped)", 0.3, "Required for access and to protect the radar during rear body panel replacement. Recalibration required after reinstall."),
                ("R&I Rear Bumper Brackets", 0.1, "Required to remove the bumper brackets attached to the body for rear body panel replacement."),
                ("R&I Rear Tail Lamps", 0.4, "Required for access to the rear body panel and to protect the lamps during welding."),
                ("R&I Battery (if equipped in rear)", 0.5, "Required if the battery is located in the rear of the vehicle to protect it during welding."),
            ]
            for desc, hrs, why in access_ops:
                # Check if this specific operation is already on the estimate (flexible match)
                desc_lower = desc.lower()
                # Extract key words from the description
                key_words = [w for w in desc_lower.replace("r&i ", "").replace("(if equipped)", "").replace("(weld zone)", "").replace("(", " ").replace(")", " ").split() if len(w) > 2]
                # Skip if any significant keyword from the description is already in the estimate
                already_present = any(kw in ops_text for kw in key_words[:2])  # Check first 2 significant words
                if not already_present:
                    self._add("Remove & Install", desc, hrs, self.body_rate, why, trigger="Rear body panel replacement")
            # Weld operations
            if "weld burn" not in ops_text and "weldburn" not in ops_text:
                self._add("Repair Procedures", "Weld Burn Rpr/Refn (adjacent panels)", 0.5, self.body_rate,
                          "Required to repair and refinish weld burn on adjacent panels after welding the rear body panel. CCC lists weld zone refinish as not included in the panel replacement time.",
                          trigger="Rear body panel replacement")
            if "test weld" not in ops_text:
                self._add("Repair Procedures", "Test Weld", 0.3, self.body_rate,
                          "Required to verify weld quality and settings before welding the rear body panel. Destructive weld testing is not included in the panel replacement time.",
                          trigger="Rear body panel replacement")
            # 2nd color setup for structural color
            if "color" not in ops_text or "structural" not in ops_text:
                self._add("Repair Procedures", "2nd Color Set-up (structural color)", 0.5, self.paint_rate,
                          "Required when the structural color (e-coat / primer color) differs from the exterior color. A second color mix is needed for the interior/structural surfaces of the rear body panel.",
                          trigger="Rear body panel replacement")

        # Rule: Wet-sand, de-nib, and precision buff for black finishes
        if any(x in ops_text for x in ["refn", "refinish", "blend", "blnd", "clear coat", "paint"]):
            exterior = (self.data.get("exterior_color") or "").lower()
            is_black = "black" in exterior and "not black" not in exterior
            if is_black and "wet sand" not in ops_text and "denib" not in ops_text and "de-nib" not in ops_text and "buff" not in ops_text:
                self._add("Repair Procedures", "Wet-sand, de-nib & precision buff (black)", 0.8, self.paint_rate,
                          "Required on black finishes. Black shows every imperfection, so wet-sanding, de-nibbing, and a precision buff are needed for a proper finish.",
                          trigger="Black exterior color")

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

        # Matte paint special considerations
        exterior = (self.data.get("exterior_color") or "").lower()
        if "matte" in exterior and any(x in ops_text for x in ["refn", "refinish", "blend", "blnd", "clear coat", "paint"]):
            if "matte clear" not in ops_text and "matte finish" not in ops_text:
                self._add("Repair Procedures", "Matte finish special procedures", 0.0, 0.0,
                          "OEM refinish procedures for matte finishes prohibit wet-sanding, de-nibbing, and buffing — these operations destroy the matte appearance. Use OEM-approved matte clear coat and follow the OEM refinish procedure exactly. Cited in OEM paint and refinish procedures for matte finishes.",
                          trigger="Matte exterior color")
                self.suggestions[-1]["amount"] = 0.00

        # Blend-specific triggers
        # Blending the right rear door
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if ("rear door" in line_text or "rt door" in line_text or "right door" in line_text) and any(x in line_text for x in ["blnd", "blend"]):
                break
        # Blending the roof rail or A-pillar
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if any(x in line_text for x in ["roof rail", "a-pillar", "a pillar", "aperture"]) and any(x in line_text for x in ["blnd", "blend"]):
                break
        
        # Feather, Prime & Block — 20% of body labor time for repaired panels
        # CCC: Feather, prime and block is not included in the repair time.
        # P-Page code Q_PC952_US: 0.6 paint (baseline), but calculate as 20% of repair labor
        repair_hours = 0.0
        for item in self.data.get("line_items", []):
            op = (item.get("operation") or "").lower()
            desc = item.get("description") or ""
            if "rpr" in op or "repair" in op:
                # Extract hours from description (e.g., "5.0" or "0.5")
                import re
                hrs_match = re.search(r"(\d+\.?\d*)\s*(?:hr|hour)?", desc)
                if hrs_match:
                    repair_hours += float(hrs_match.group(1))
        has_refinish = any(x in ops_text for x in ["refn", "refinish", "paint", "clear coat"])
        if repair_hours > 0 and has_refinish and "feather" not in ops_text and "prime" not in ops_text and "block" not in ops_text:
            fpb_hours = round(repair_hours * 0.20, 1)
            if fpb_hours < 0.3:
                fpb_hours = 0.3  # Minimum
            self._add("Repair Procedures", "Feather, Prime & Block", fpb_hours, self.paint_rate,
                      f"Feather, prime and block is required to take the repaired area from 150 grit to 320 grit to prepare the panel for the refinish process. Calculated at 20% of the body labor time to repair the panel ({repair_hours} hr × 20% = {fpb_hours} hr refinish). CCC lists feather, prime and block as not included in the repair time. P-Page code Q_PC952_US.",
                      trigger="Repaired panel with refinish")
            # Primer material — calculated at material rate × refinish hours
            material_rate = self.data.get("labor_rates", {}).get("paint_supplies", 40.0)
            primer_amount = round(fpb_hours * material_rate, 2)
            self._add("Parts Differences", "Primer (Feather, Prime & Block)", 0.0, 0.0,
                      f"Primer is one of the most expensive materials used in a body shop. Required to build the repaired area from 150 grit to 320 grit before refinish. Calculated at {fpb_hours} hr × ${material_rate:.2f}/hr material rate = ${primer_amount:.2f}.",
                      trigger="Repaired panel with refinish")
            self.suggestions[-1]["amount"] = primer_amount
            self.suggestions[-1]["description"] += f" — ${primer_amount:.2f}"

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
            # Only applies to SUVs with 3rd row seating or multi-passenger vans/minivans
            ymm = (self.data.get("year_make_model") or "").upper()
            is_suv_or_van = any(x in ymm for x in ["SUV", "TAHOE", "SUBURBAN", "EXPEDITION", "NAVIGATOR", "ESCALADE", "YUKON", "SEQUOIA", "ARMADA", "QX80", "GX", "LX", "PILOT", "HIGHLANDER", "EXPLORER", "DURANGO", "TRAVERSE", "ENCLAVE", "ACADIA", "TELLURIDE", "PALISADE", "CARNIVAL", "ODYSSEY", "SIENNA", "PACIFICA", "VOYAGER", "SEDONA", "QUEST", "VAN", "MINIVAN"])
            # Only check for 3rd row if build sheet is present
            has_3rd_row = False
            if self.build_sheet:
                has_3rd_row = self._has_option(["3rd row", "third row", "rear ac", "rear a/c", "rear climate", "tri-zone", "tri zone", "captain"])
            if is_suv_or_van or has_3rd_row:
                if "evacuation" not in ops_text and "recharge" not in ops_text and "evacuate" not in ops_text:
                    self._add("Repair Procedures", "Evacuate and recharge rear AC system", 1.5, self.mech_rate,
                              "Required when the rear AC unit is removed for access on a quarter panel replacement. The system must be evacuated before disconnecting lines and recharged after reassembly. Only applies to SUVs with 3rd row seating or multi-passenger vans/minivans.",
                              trigger="Quarter panel replacement + rear AC equipped (SUV/van with 3rd row)")
                if "rear ac" not in ops_text and "rear a/c" not in ops_text:
                    self._add("Repair Procedures", "R&I Rear AC unit (quarter panel access)", 1.8, self.mech_rate,
                              "Required for access when replacing the quarter panel on the side of the rear AC unit. The unit sits against the quarter and must be removed to reach the weld zone. Only applies to SUVs with 3rd row seating or multi-passenger vans/minivans.",
                              trigger="Quarter panel replacement + rear AC equipped (SUV/van with 3rd row)")

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
            if "chmsl" not in ops_text and "high-mount" not in ops_text and "stop lamp" not in ops_text and lid_name == "liftgate":
                self._add("Repair Procedures", "R&I High-Mount Stop Lamp (CHMSL)", 0.2, self.body_rate,
                          "Required for access when repairing or refinishing the liftgate. Only applies to true liftgates, not trunk lids or deck lids.",
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
                ymm = (self.data.get("year_make_model") or "").upper()
                is_suv_or_van = any(x in ymm for x in ["SUV", "TAHOE", "SUBURBAN", "EXPEDITION", "NAVIGATOR", "ESCALADE", "YUKON", "SEQUOIA", "ARMADA", "QX80", "GX", "LX", "PILOT", "HIGHLANDER", "EXPLORER", "DURANGO", "TRAVERSE", "ENCLAVE", "ACADIA", "TELLURIDE", "PALISADE", "CARNIVAL", "ODYSSEY", "SIENNA", "PACIFICA", "VOYAGER", "SEDONA", "QUEST", "VAN", "MINIVAN"])
                if is_suv_or_van:
                    self._add("Repair Procedures", "R&I Video Display (Second / Third Row)", 0.4, self.body_rate,
                              "Required for access when removing the headliner on vehicles equipped with rear seat entertainment. GM Video Display Replacement is part of the headliner R&I. Only applies to SUVs and minivans.",
                              trigger="Quarter panel replacement / sectioning (SUV/van)")
            if "sir" not in ops_text and "disable" not in ops_text:
                self._add("Repair Procedures", "SIR Disabling and Enabling", 0.5, self.mech_rate,
                          "Required before any work on or near the SRS / curtain air bag. GM SIR Disabling and Enabling procedure must be followed to prevent accidental deployment.",
                          trigger="Quarter panel replacement / sectioning")
            if "courtesy lamp" not in ops_text and "reading lamp" not in ops_text:
                self._add("Repair Procedures", "R&I Rear Seat Position Center Reading and Courtesy Lamp", 0.2, self.body_rate,
                          "Required for access when removing the headliner. GM procedure is part of the headlining trim panel replacement for quarter panel sectioning.",
                          trigger="Quarter panel replacement / sectioning")

        # Roof rail refinish → blend quarter panel + related R&I
        roof_rail_refn = False
        for item in self.data.get("line_items", []):
            line_text = (item.get("description", "") + " " + item.get("operation", "")).lower()
            if "roof rail" in line_text and any(x in line_text for x in ["refn", "refinish", "rpr", "repair"]):
                roof_rail_refn = True
                break
        if roof_rail_refn:
            if "quarter" not in ops_text or "blnd" not in ops_text:
                self._add("Repair Procedures", "Blend Quarter Panel (extend clear from roof rail)", 1.5, self.paint_rate,
                          "CCC lists extension of clear coat to adjacent panels as not included. Refinish on the roof rail requires the clear coat to be extended to the nearest breaking point, which is the quarter panel. Blend the quarter panel and clear the entire panel to avoid a visible blend line. Cited in CCC MOTOR Guide to Estimating — Clear Coat Finishes and Extension of Clear Coat.",
                          trigger="Roof rail refinish")
            if "bumper cover" not in ops_text and "rear bumper" not in ops_text:
                self._add("Remove & Install", "R&I Rear Bumper Cover", 0.8, self.body_rate,
                          "Required for access to blend the quarter panel and to protect the bumper from overspray.",
                          trigger="Blend quarter panel")
            if "tail lamp" not in ops_text and "taillamp" not in ops_text:
                self._add("Remove & Install", "R&I RT Tail Lamp", 0.3, self.body_rate,
                          "Required for access to blend the quarter panel and to protect the lamp from overspray.",
                          trigger="Blend quarter panel")
            if "quarter glass" not in ops_text and "qtr glass" not in ops_text:
                self._add("Remove & Install", "R&I RT Quarter Glass", 0.6, self.body_rate,
                          "Required for access to blend the quarter panel and to protect the glass from overspray and refinish materials.",
                          trigger="Blend quarter panel")
                self._add("Miscellaneous", "Glass Kit for Quarter Glass", 0.0, 0.0,
                          "Required when R&I of the quarter glass. Includes urethane and primers for proper reseal.",
                          trigger="R&I Quarter Glass")
                self.suggestions[-1]["amount"] = 45.00
            if "wheelhouse liner" not in ops_text and "wheel liner" not in ops_text and "rear wheel" not in ops_text:
                self._add("Remove & Install", "R&I RT Rear Wheelhouse Liner", 0.3, self.body_rate,
                          "Required for access to the quarter panel blend area and to protect the liner from overspray.",
                          trigger="Blend quarter panel")
            if "mask jamb" not in ops_text and "jamb" not in ops_text:
                self._add("Miscellaneous", "Mask Jambs / Openings", 0.5, self.body_rate,
                          "Required to protect door and liftgate openings from overspray when blending the quarter panel.",
                          trigger="Blend quarter panel")
            if "mask for primer" not in ops_text and "mask primer" not in ops_text:
                self._add("Miscellaneous", "Mask for Primer", 0.3, self.paint_rate,
                          "Required to protect adjacent panels and openings when priming the blend area.",
                          trigger="Blend quarter panel")
            if "pre-clean" not in ops_text and "pre clean" not in ops_text and "pre-wash" not in ops_text:
                self._add("Miscellaneous", "Pre-Clean for Disassembly", 0.5, self.body_rate,
                          "Required before disassembly to remove dirt and road film so materials and refinish adhere properly.",
                          trigger="Any collision repair")
            if "spray-out" not in ops_text and "spray out" not in ops_text and "sprayout" not in ops_text:
                self._add("Repair Procedures", "Spray-out Cards", 0.5, self.paint_rate,
                          "Before any color touches the car, the painter sprays test cards to confirm the mixed color actually matches under the booth lights. Required on non-black finishes.",
                          trigger="Refinish / blend operation")

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
        needs_alignment = False
        
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
                      "Structural damage diagnosis and vehicle set up time is listed in CCC's Labor Time Premise as not included in any estimated work time. A collision can push the body structure out of its factory dimensions, so the vehicle is set up and measured before the repair plan is final. Cited in CCC MOTOR Guide to Estimating — Labor Time Premise.",
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
                      "Required for disposal of paint waste, solvents, old parts, and other regulated materials. P-Page code 10.")
            self.suggestions[-1]["amount"] = 5.00  # P-Page code 10
            self.suggestions[-1]["description"] += " — $5.00"
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
        # Handle calls that pass why as the 4th positional arg (no rate)
        if isinstance(rate, str):
            trigger = why if why else trigger
            why = rate
            rate = self.body_rate
        if rate is None:
            rate = self.body_rate
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


def analyze_estimate(pdf_path: str, build_sheet_path: Optional[str] = None, oem_procedures_path: Optional[str] = None) -> Dict:
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
    
    # OEM Procedures analysis (if provided)
    if oem_procedures_path:
        oem_suggestions = _analyze_oem_procedures(oem_procedures_path, data)
        suggestions.extend(oem_suggestions)
    
    total_missed = sum(s["amount"] for s in suggestions if s["selected"])
    
    return {
        "estimate": data,
        "build_sheet": build_sheet,
        "suggestions": suggestions,
        "total_potential": total_missed,
        "suggestion_count": len(suggestions)
    }


def _analyze_oem_procedures(oem_path: str, estimate_data: Dict) -> List[Dict]:
    """Extract R&I procedures, one-time use parts, and position statements from OEM PDF."""
    import pdfplumber
    suggestions = []
    try:
        with pdfplumber.open(oem_path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return suggestions
    
    text_lower = text.lower()
    ops_text = " ".join([f"{i['operation']} {i['description']}" for i in estimate_data.get("line_items", [])]).lower()
    
    # Look for one-time use / non-reusable parts
    if "one-time" in text_lower or "one time use" in text_lower or "non-reusable" in text_lower or "cannot be reused" in text_lower:
        if "one-time" not in ops_text and "one time" not in ops_text:
            suggestions.append({
                "category": "Parts Differences",
                "description": "One-time use parts (OEM procedures)",
                "why": "OEM repair procedures list one-time use or non-reusable fasteners/clips. These must be replaced and are not included in the base labor time.",
                "hours": 0.0,
                "amount": 0.0,
                "trigger": "OEM procedures PDF",
                "selected": True
            })
    
    # Look for ADAS / calibration mentions
    if any(x in text_lower for x in ["calibration", "adas", "radar", "camera", "sensor"]):
        if "calibrat" not in ops_text:
            suggestions.append({
                "category": "Pre/Post Scans",
                "description": "ADAS calibration (from OEM procedures)",
                "why": "OEM repair procedures reference ADAS components or calibrations. Verify which calibrations are required for this repair and add them if not present.",
                "hours": 0.0,
                "amount": 0.0,
                "trigger": "OEM procedures PDF",
                "selected": True
            })
    
    # Look for R&I procedures that may be missing
    for part in ["headliner", "overhead console", "kick panel", "sunshade", "garnish"]:
        if part in text_lower and part not in ops_text:
            suggestions.append({
                "category": "Repair Procedures",
                "description": f"R&I {part.title()} (from OEM procedures)",
                "why": f"OEM repair procedures list R&I of the {part} as part of the repair access. This operation is not present on the estimate.",
                "hours": 0.3,
                "amount": 0.0,
                "trigger": "OEM procedures PDF",
                "selected": True
            })
    
    return suggestions


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = analyze_estimate(sys.argv[1])
        print(json.dumps(result, indent=2))
    else:
        print("Usage: python estimate_analyzer.py <pdf_path>")
