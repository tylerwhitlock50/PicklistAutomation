"""Blueprint registration. Each module exposes a ``bp``."""
from flask import Flask

from picklist.routes import runs, settings, audit, serial, allocation, shipping, orders, lookup, request_queue, pick, verify


def register_blueprints(app: Flask) -> None:
    for module in (runs, settings, audit, serial, allocation, shipping, orders, lookup, request_queue, pick, verify):
        app.register_blueprint(module.bp)
