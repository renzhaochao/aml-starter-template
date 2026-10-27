# Build from the repository root with: python -m PyInstaller packaging/windows/aml-server.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).resolve().parents[1]
a = Analysis(
    [str(root / "server.py")],
    pathex=[str(root)],
    binaries=[],
    datas=collect_data_files("jieba"),
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on",
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "httpx", "watchfiles", "httptools", "websockets", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="aml-server", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
    version=str(root / "packaging" / "windows" / "version.txt"),
)
