from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_server.app import AppSettings, create_app
from starlette.routing import WebSocketRoute


def client(tmp_path) -> TestClient:
    return TestClient(create_app(AppSettings(data_dir=tmp_path)))


def test_health_reports_ok_and_a_version(tmp_path):
    response = client(tmp_path).get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


def test_an_unknown_api_route_is_a_404_not_an_index_page(tmp_path):
    # The SPA is mounted at the root later; /api must never fall through to it,
    # or a typo'd endpoint returns 200 and HTML and the browser cannot tell.
    assert client(tmp_path).get("/api/nope").status_code == 404


def test_the_data_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "data"
    create_app(AppSettings(data_dir=target))

    assert target.is_dir()


def test_nothing_squats_outside_the_api_prefix_but_the_sockets(tmp_path):
    """The root belongs to the SPA, which is mounted in a later task.

    A route left at its default -- /redoc, /docs/oauth2-redirect -- is a page the
    interface will one day claim, answered by FastAPI instead, and nothing about
    the symptom points back here.

    The two sockets are the exception the design writes down: `/ws/daemon` is
    dialled by a program on a Pi and `/ws/ui` by the SPA itself, and neither can
    be moved under a prefix after the fact. So they are allowed by shape --
    a websocket under /ws/ -- rather than by name, and anything else outside
    /api is still a collision.

    `iter_route_contexts` rather than `app.routes`, which FastAPI resolves
    lazily: walking it directly finds no included route at all, so this could
    not see a socket a later task hangs off the root.
    """
    app = create_app(AppSettings(data_dir=tmp_path))
    routes = [
        (context.path or context.original_route.path, context.original_route)
        for context in iter_route_contexts(app.routes)
    ]

    outside = {
        path
        for path, route in routes
        if not path.startswith("/api")
        and not (isinstance(route, WebSocketRoute) and path.startswith("/ws/"))
    }
    assert outside == set(), f"these would collide with the SPA: {sorted(outside)}"
