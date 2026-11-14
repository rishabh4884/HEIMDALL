"""HEIMDALL scientific engine.

Modules:
    config      versioned engine configuration (provisional defaults)
    stats       log-space p-values, two-sided sigma conversion, intervals
    skygeom     spherical geometry, tangent planes, covariance ellipses
    psf         PSF models (ePSF, legacy PSF cube, Gaussian approximation)
    background  mesh background estimation
    detection   matched-filter detection, PSF-fit and forced photometry
    association cross-observation association with explicit ambiguity
    motion      stationary/linear motion test, parallax-motion fit
    ephemeris   observer heliocentric positions
    spectral    spectrum assembly and comparisons
    temporal    epochs, comparable series, variability, playback sequences
    linking     transient selection and moving-object linking
    reasoning   checks, alternatives, attention levels, statements
    planetx     distant-body hypothesis evaluation
    validation  executable validation procedures and the validation registry

The engine is independent of any user interface.
"""
