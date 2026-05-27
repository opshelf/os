from setuptools import setup, find_packages

setup(
    name="hardware",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.8",
    install_requires=[
        "numpy>=1.19.0",
        "opencv-python>=4.5.0",
        "pymodbus>=3.0.0",
        "pytest>=7.0.0",
        "pytest-asyncio>=0.20.0",
    ],
    extras_require={
        "dev": [
            "pytest",
            "pytest-asyncio",
            "pytest-cov",
            "black",
            "isort",
            "mypy",
        ]
    }
)
