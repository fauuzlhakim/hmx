from setuptools import find_packages, setup

setup(
    name="hermes-mux",
    version="0.1.0",
    description="Single-home multi-account manager for Hermes Codex auth pools",
    py_modules=["hmx"],
    packages=find_packages(include=["hmxlib", "hmxlib.*"]),
    install_requires=["PyYAML>=6.0"],
    extras_require={"dev": ["pytest>=8.0"]},
    entry_points={"console_scripts": ["hmx=hmx:main"]},
)
