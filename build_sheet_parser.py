#!/usr/bin/env python3
"""
Parser for CCC Vehicle Equipment Reports (build sheets).
Extracts installed options so the analyzer can make more accurate suggestions.
"""

import re
import pdfplumber
from typing import Dict, List, Optional


class BuildSheetParser:
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.raw_text = ""
        self.vehicle = ""
        self.vin = ""
        self.installed_options: List[str] = []

    def extract_text(self) -> str:
        with pdfplumber.open(self.pdf_path) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            self.raw_text = "\n".join(pages)
        return self.raw_text

    def parse(self) -> Dict:
        if not self.raw_text:
            self.extract_text()

        # Vehicle and VIN
        vin_match = re.search(r"VIN:\s*([A-HJ-NPR-Z0-9]{17})", self.raw_text, re.I)
        if vin_match:
            self.vin = vin_match.group(1)

        veh_match = re.search(r"Vehicle:\s*(.+)", self.raw_text)
        if veh_match:
            self.vehicle = veh_match.group(1).strip().split("\n")[0]

        # Installed options — look for lines with " - inst" or checkmarks
        lines = self.raw_text.split("\n")
        for line in lines:
            line_clean = line.strip()
            # Pattern: option name followed by - inst
            if " - inst" in line_clean.lower() or "-inst" in line_clean.lower():
                # Split on - inst and take the part before
                parts = re.split(r"\s*-\s*inst", line_clean, flags=re.I)
                for part in parts[:-1]:  # everything before the last -inst
                    opt = part.strip()
                    # Remove leading checkmarks and numbers that are artifacts
                    opt = re.sub(r"^[✓✔0-9\s]+", "", opt).strip()
                    # Split multi-item lines on common separators
                    sub_opts = re.split(r"\s{2,}|\s-\s", opt)
                    for so in sub_opts:
                        so = so.strip()
                        if so and len(so) > 2 and so not in self.installed_options:
                            self.installed_options.append(so)

            # Also catch "inst" at end of short lines
            if line_clean.lower().endswith(" inst") or line_clean.lower().endswith("-inst"):
                opt = re.sub(r"\s*-?\s*inst\s*$", "", line_clean, flags=re.I).strip()
                opt = re.sub(r"^[✓✔]\s*", "", opt).strip()
                if opt and len(opt) > 2 and opt not in self.installed_options:
                    self.installed_options.append(opt)

        return {
            "vehicle": self.vehicle,
            "vin": self.vin,
            "installed_options": sorted(set(self.installed_options)),
            "raw_text": self.raw_text[:5000],  # keep a sample
        }


def parse_build_sheet(pdf_path: str) -> Dict:
    return BuildSheetParser(pdf_path).parse()


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/workspace/attachments/Vehicle Equipment Report (1).pdf"
    result = parse_build_sheet(path)
    print("Vehicle:", result["vehicle"])
    print("VIN:", result["vin"])
    print("Installed options:")
    for opt in result["installed_options"]:
        print(" -", opt)
