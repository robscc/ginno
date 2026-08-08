"""HTTP API routers for the Ginno runtime sidecar.

Each module owns one domain of endpoints and registers them on a FastAPI
``APIRouter``; server.py includes the routers. Shared process state lives in
``server_shared`` so these modules never import server.py (import cycle).
"""
