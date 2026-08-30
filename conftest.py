"""Root conftest.

Exists so pytest puts the repository root on sys.path: with the default
"prepend" import mode, the directory containing the topmost conftest.py is
inserted, which is what makes `import computer_use` resolve from tests/
without installing the package or setting PYTHONPATH by hand.
"""
