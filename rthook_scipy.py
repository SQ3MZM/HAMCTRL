#!/usr/bin/env python3
"""
rthook_scipy.py — runtime hook fixing scipy.stats under PyInstaller + Py3.12.

PROBLEM (confirmed via traceback):
scipy/stats/_distn_infrastructure.py ends with:
    for obj in [s for s in dir() if s.startswith('_doc_')]:
        exec('del ' + obj)
    del obj          # <- NameError under Py3.12 + PyInstaller frozen importer

Under Python 3.12 the loop variable 'obj' is not visible after the loop
when the module is loaded through PyInstaller's frozen importer
(pyimod02_importers), so the final 'del obj' raises NameError and the
app crashes on importing scipy.

FIX:
PyInstaller uses its OWN loader (in pyimod02_importers), not the standard
SourceFileLoader. We patch its exec_module: when it loads
_distn_infrastructure, inject 'obj' into the module's globals BEFORE the
module code runs, so the trailing 'del obj' has something to delete.
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
