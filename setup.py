from setuptools import setup, find_packages

setup(
    name="oracle-migrator",
    version="1.0.0",
    description="Oracle Forms & Reports complexity analyzer and Java/JasperReports converter",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=["flask>=3.0.0"],
    extras_require={"dev": ["pytest>=7.4.0"]},
    entry_points={
        "console_scripts": [
            "oracle-migrator=cli:main",
        ]
    },
)
