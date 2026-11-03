"""Offline reverse geocoding with GeoNames cities1000 (a stand-in until Photon runs on Mimir, P11).

The dump is downloaded once into the models directory; coordinates are never sent anywhere.
"""

import io
import math
import zipfile
from pathlib import Path

import httpx
import numpy as np

BASE = "https://download.geonames.org/export/dump/"

# GeoNames admin1 names for Poland -> the Polish voivodeship names.
PL_VOIVODESHIPS = {
    "Lower Silesia": "dolnośląskie", "Kujawsko-Pomorskie": "kujawsko-pomorskie", "Kuyavia-Pomerania": "kujawsko-pomorskie",
    "Łódź Voivodeship": "łódzkie", "Lodz Voivodeship": "łódzkie", "Lublin": "lubelskie", "Lubusz": "lubuskie",
    "Lesser Poland": "małopolskie", "Mazovia": "mazowieckie", "Opole Voivodeship": "opolskie",
    "Subcarpathia": "podkarpackie", "Podlasie": "podlaskie", "Pomerania": "pomorskie", "Silesia": "śląskie",
    "Świętokrzyskie": "świętokrzyskie", "Swietokrzyskie": "świętokrzyskie", "Warmia-Masuria": "warmińsko-mazurskie",
    "Greater Poland": "wielkopolskie", "West Pomerania": "zachodniopomorskie",
}


class Geocoder:
    def __init__(self, models_dir: Path):
        d = models_dir / "geonames"
        d.mkdir(parents=True, exist_ok=True)
        for name in ("cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt"):
            if not (d / name).exists():
                r = httpx.get(BASE + name, timeout=300, follow_redirects=True)
                r.raise_for_status()
                (d / name).write_bytes(r.content)
        with zipfile.ZipFile(d / "cities1000.zip") as z:
            text = z.read("cities1000.txt").decode("utf-8")
        names, lat, lon, cc, adm1 = [], [], [], [], []
        for line in io.StringIO(text):
            f = line.rstrip("\n").split("\t")
            names.append(f[1])
            lat.append(float(f[4]))
            lon.append(float(f[5]))
            cc.append(f[8])
            adm1.append(f[10])
        self.names, self.cc, self.adm1 = names, cc, adm1
        self.lat, self.lon = np.radians(np.array(lat)), np.radians(np.array(lon))
        self.admin1 = {}
        for line in (d / "admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
            code, name, ascii_, _ = line.split("\t")
            self.admin1[code] = PL_VOIVODESHIPS.get(name, PL_VOIVODESHIPS.get(ascii_, name))
        self.country = {}
        for line in (d / "countryInfo.txt").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                f = line.split("\t")
                self.country[f[0]] = f[4]

    def lookup(self, lat: float, lon: float) -> dict:
        p1, l1 = math.radians(lat), math.radians(lon)
        h = np.sin((self.lat - p1) / 2) ** 2 + np.cos(p1) * np.cos(self.lat) * np.sin((self.lon - l1) / 2) ** 2
        dist = 2 * 6371 * np.arcsin(np.sqrt(h))
        i = int(dist.argmin())
        cc = self.cc[i]
        return {
            "place": self.names[i],
            "region": self.admin1.get(f"{cc}.{self.adm1[i]}"),
            "country": "Polska" if cc == "PL" else self.country.get(cc, cc),
            "km": round(float(dist[i]), 1),
        }
