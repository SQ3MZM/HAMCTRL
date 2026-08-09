"""
rigs/ — profile sterowania dla poszczegolnych modeli radia CI-V.

Kazdy model w SCOPE_MODELS (config.py) ma odpowiadajacy profil w civ_profiles.py.
Profil okresla roznice miedzy modelami: domyslny adres CI-V, predkosc, mapowanie
trybow, parametry scope (max amplitude, dlugosc naglowka).

civ.py laduje profil przez get_civ_profile(model_id) i uzywa go zamiast
hardcoded stalych — dzieki temu dodanie/poprawienie modelu nie wymaga
modyfikacji glownej logiki w civ.py.
"""
from .civ_profiles import get_civ_profile, CIV_PROFILES, DEFAULT_PROFILE

__all__ = ["get_civ_profile", "CIV_PROFILES", "DEFAULT_PROFILE"]
