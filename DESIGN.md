# Workbench design

The full-width DR1 image is the first view. Keep the layers, version-aware inspector, and charts in panels below the map, with one panel open at a time. A chart mark and a map mark should lead to the same observation record when they refer to the same measurement.

- Paper `#f1f5f5`, surface `#ffffff`, ink `#102934`, secondary text `#4f6670`, rules `#d7e2e4`.
- The map has its own plotting surface in `#101d29`. Use gold for measured positions, pale blue for sources, pink for candidates, and teal for time-ordered linkages.
- Use consistent sans-serif text for controls and monospace only for coordinates, identifiers and other exact values.
- Keep warnings and scientific qualification readable alongside the result. The state of a computation and the strength of a scientific claim get their own labels.
- Fit the complete Galactic ellipse at the widest zoom, independently of the sky longitude and viewport shape. On narrow screens stack the investigation library and inspector below the viewport.
