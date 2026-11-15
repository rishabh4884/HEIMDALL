# Credits, data acknowledgements and citations

HEIMDALL is an independent project made for the NASA Space Apps Challenge. It is not endorsed by, affiliated with, or an official product of NASA, JPL, Caltech/IPAC, ESA or CDS. Mission and agency names are used only to identify the data sources. HEIMDALL does not use the NASA insignia or any agency logo.

HEIMDALL's own source code is released under the MIT License (see `LICENSE`). The license covers only HEIMDALL's code, not the third-party data or services listed below; each of those stays under its provider's terms. No archive data, catalogue file or downloaded image is committed to this repository. Everything is fetched at run time from the provider and cached locally.

## Data and services

| Source | Used for | Acknowledgement / citation |
|---|---|---|
| **SPHEREx** (NASA mission; JPL and Caltech) | Level 2 spectral images, the DR1 all-sky spectral cube, ePSF calibration | "This publication makes use of data products from the Spectro-Photometer for the History of the Universe, Epoch of Reionization and Ices Explorer (SPHEREx), which is a joint project of the Jet Propulsion Laboratory and the California Institute of Technology, and is funded by the National Aeronautics and Space Administration." Mission: Doré et al. 2014, arXiv:1412.4872. Data: SPHEREx DR1 Spectral Images, [doi:10.26131/IRSA678](https://doi.org/10.26131/IRSA678); SPHEREx DR1 Spectral All-Sky Cube, [doi:10.26131/IRSA679](https://doi.org/10.26131/IRSA679). |
| **NASA/IPAC Infrared Science Archive (IRSA)** | Archive search (SIA2, TAP), image and cutout access, HiPS imagery, Gaia and CatWISE mirrors | "This research has made use of the NASA/IPAC Infrared Science Archive, which is funded by the National Aeronautics and Space Administration and operated by the California Institute of Technology." |
| **ESA Gaia DR3** (ESA archive and the IRSA mirror) | Reference positions, proper motions and parallaxes; astrometric registration | "This work has made use of data from the European Space Agency (ESA) mission *Gaia* (https://www.cosmos.esa.int/gaia), processed by the *Gaia* Data Processing and Analysis Consortium (DPAC, https://www.cosmos.esa.int/web/gaia/dpac/consortium). Funding for the DPAC has been provided by national institutions, in particular the institutions participating in the *Gaia* Multilateral Agreement." Gaia Collaboration, Prusti et al. 2016, A&A 595, A1; Gaia Collaboration, Vallenari et al. 2023, A&A 674, A1. |
| **CatWISE2020** (via IRSA) | Infrared catalogue context | Marocco et al. 2021, ApJS 253, 8. "This publication makes use of data products from the Wide-field Infrared Survey Explorer, which is a joint project of the University of California, Los Angeles, and the Jet Propulsion Laboratory/California Institute of Technology, funded by the National Aeronautics and Space Administration, and from NEOWISE, which is a joint project of the Jet Propulsion Laboratory/California Institute of Technology and the University of Arizona, funded by the National Aeronautics and Space Administration." |
| **JPL Solar System Dynamics** (`sb_ident` API) | Known small bodies near a field and epoch | Solar System Dynamics Group, Jet Propulsion Laboratory, California Institute of Technology, https://ssd.jpl.nasa.gov/. |
| **CDS Sesame / SIMBAD** (called only when a user asks to resolve a name) | Object-name resolution | "This research has made use of the SIMBAD database, operated at CDS, Strasbourg, France." Wenger et al. 2000, A&AS 143, 9. |
| **CDS VizieR**, catalogue VI/49 (CDS and the CfA mirror) | IAU constellation boundaries | Davenhall A.C. & Leggett S.K. 1989, *Constellation Boundary Data*, CDS catalogue VI/49 (boundaries of Delporte 1930). "This research has made use of the VizieR catalogue access tool, CDS, Strasbourg, France." Ochsenbein et al. 2000, A&AS 143, 23. |
| **HiPS** (Hierarchical Progressive Surveys, IVOA standard) | Tiling scheme of the all-sky imagery | Fernique et al. 2015, A&A 578, A114. |

## Software

| Package | License | Citation |
|---|---|---|
| Astropy | BSD-3-Clause | "This research made use of Astropy, a community-developed core Python package for Astronomy." Astropy Collaboration 2013, A&A 558, A33; 2018, AJ 156, 123; 2022, ApJ 935, 167. |
| NumPy | BSD-3-Clause | Harris et al. 2020, Nature 585, 357. |
| SciPy | BSD-3-Clause | Virtanen et al. 2020, Nature Methods 17, 261. |

The browser workbench uses no third-party JavaScript, CSS, fonts or images. Its icons and logo mark are drawn for HEIMDALL.

## Using HEIMDALL results

Any report built from HEIMDALL output inherits the acknowledgement duties above for the data it used. Exported evidence bundles list their data sources and terms (`heimdall/evidence.py`, `DATA_SOURCES`). Results are provisional research outputs; see each result's stated limitations.
