# -*- mode: python ; coding: utf-8 -*-
# ============================================================
# Сборка в один .exe: pyinstaller build_exe.spec
# См. build_exe.md за пошаговой инструкцией и объяснением, почему
# именно так (частые грабли с FastAPI/uvicorn/pydantic под PyInstaller).
# ============================================================
from PyInstaller.utils.hooks import collect_all

datas = [
    ('app/schema.sql', 'app'),
    ('app/config/kit-templates.json', 'app/config'),
    ('public', 'public'),
]
hiddenimports = [
    # uvicorn выбирает конкретную реализацию цикла событий/протокола
    # динамически по строке (не через явный import) — PyInstaller не видит
    # эти модули при статическом анализе, их нужно перечислить руками.
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
]

# pydantic v2 использует скомпилированное Rust-ядро (pydantic_core) и
# собственный механизм сборки схем — без collect_all часть модулей не
# находится, сервер падает при старте с непонятным ImportError.
for pkg in ('pydantic', 'pydantic_core', 'fastapi', 'starlette'):
    d, b, h = collect_all(pkg)
    datas += d
    hiddenimports += h

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='km-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
