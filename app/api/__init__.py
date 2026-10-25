"""The API blueprint package.

NOTE: this package used to contain a second application factory,
`create_api_app()`, which built a standalone Flask app on its own port.
It was dead code -- nothing imported or called it -- and it duplicated the
config-loading, SECRET_KEY validation and blueprint-registration logic
that `app.create_app()` already owned. Two factories meant two places
for the configuration contract to drift. It was removed on 2026-10-25
(security sweep S10).

The application factory lives in `app/__init__.py`: `create_app()`.
"""
