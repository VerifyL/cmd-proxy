from setuptools import setup, find_packages

with open("README", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="cmd-proxy",
    version="1.0.0",
    author="Ryen",
    author_email="tennshi520@gmail.com",
    description="Unix socket based command proxy for containers",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your/cmd-proxy",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
    ],
    python_requires=">=3.6",
    install_requires=[
        "pyyaml>=5.4.1",
    ],
    entry_points={
        "console_scripts": [
            "cmd-proxy-server=cmd_proxy.server:main",
        ],
    },
    include_package_data=True,
)
