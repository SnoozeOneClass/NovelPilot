"""Database infrastructure owned by the NovelPilot application lifespan.

Import concrete modules directly. Keeping package initialization side-effect free avoids
making the Unit of Work and domain command layers depend on one another during import.
"""
