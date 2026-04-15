"""Drawing Synchronization System for Electrical Engineering Drawings.

Monitors PDF and CAD (DWG/DXF) drawings for component changes,
detects mismatches across drawings, and enables synchronous updates.
"""

__version__ = "1.0.0"

try:
    from .drawing_classifier import DrawingClassifier
except ImportError:
    DrawingClassifier = None
