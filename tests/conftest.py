from src import textshape

# Subtitle and brand graphics need raqm (HarfBuzz) for Arabic — load it before any test imports Pillow's fonts.
textshape.ensure()
