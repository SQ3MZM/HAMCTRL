#!/usr/bin/env python3
"""
rthook_scipy.py — runtime hook naprawiajacy scipy.stats pod PyInstaller + Py3.12.

PROBLEM (potwierdzony sladem bledu):
scipy/stats/_distn_infrastructure.py konczy sie:
    for obj in [s for s in dir() if s.startswith('_doc_')]:
        exec('del ' + obj)
    del obj          # <- NameError pod Py3.12 + PyInstaller frozen importer

Pod Python 3.12 zmienna petli 'obj' nie jest widoczna po petli gdy modul
ladowany przez frozen importer PyInstallera (pyimod02_importers), wiec
koncowe 'del obj' rzuca NameError i aplikacja pada na imporcie scipy.

ROZWIAZANIE:
PyInstaller uzywa WLASNEGO loadera (w pyimod02_importers), nie standardowego
SourceFileLoader. Patchujemy jego exec_module: gdy laduje
_distn_infrastructure, wstrzykujemy 'obj' do globals modulu PRZED wykonaniem
kodu - dzieki czemu koncowe 'del obj' ma co usunac.
"""


def _install():
    candidates = []
    try:
        import pyimod02_importers as _pyi
        candidates.append(_pyi)
    except Exception:
        pass
    try:
        from PyInstaller.loader import pyimod02_importers as _pyi2
        candidates.append(_pyi2)
    except Exception:
        pass

    for mod in candidates:
        for attr in dir(mod):
            cls = getattr(mod, attr, None)
            if not isinstance(cls, type):
                continue
            if not hasattr(cls, "exec_module"):
                continue
            _orig = cls.exec_module
            if getattr(_orig, "_scipy_patched", False):
                continue

            def _make(orig):
                def _patched(self, module):
                    name = getattr(module, "__name__", "") or ""
                    if name.endswith("_distn_infrastructure"):
                        try:
                            module.__dict__.setdefault("obj", None)
                        except Exception:
                            pass
                    return orig(self, module)
                _patched._scipy_patched = True
                return _patched

            try:
                cls.exec_module = _make(_orig)
            except Exception:
                pass


_install()
