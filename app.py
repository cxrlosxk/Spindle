"""PyInstaller-safe application entry point."""
from spindle.ui import launch

if __name__ == '__main__':
    raise SystemExit(launch())
