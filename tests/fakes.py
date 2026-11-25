"""Stand-ins shared by the tests."""

from app.config import UpdatesConfig


def update_settings(**overrides) -> UpdatesConfig:
    """Update-check settings for a test: only `latest` floats unless the test
    says otherwise, whatever the environment the tests run in."""
    return UpdatesConfig(**{"floating_tags": ["latest"], **overrides})


class FakePortainer:
    """A Portainer described by plain attributes, for anything that takes a
    PortainerClient. Fill in what the test is about; every call is recorded
    in `calls`, and a method named in `failing` raises instead of answering.

        lab = FakePortainer()
        lab.add_stack(1, "web", file="services: {web: {image: nginx:latest}}")
        lab.add_container(1, "web-1", "nginx:latest", stack="web")
    """

    def __init__(self, name: str = "lab", endpoints: dict[int, str] | None = None):
        self.instance = type("Instance", (), {"name": name})()
        self.endpoints = [{"Id": i, "Name": n} for i, n in (endpoints or {1: "local"}).items()]
        self.stacks: list[dict] = []
        self.stack_files: dict[int, str] = {}
        self.containers: dict[int, list[dict]] = {e["Id"]: [] for e in self.endpoints}
        self.container_info: dict[str, dict] = {}   # container id -> inspect
        self.image_info: dict[str, dict] = {}       # image name or id -> inspect
        # "list_endpoints" -> exception, or ("list_containers", 2) for one argument.
        self.failing: dict = {}
        self.calls: list[tuple] = []
        self.reconnected = 0

    # --- describing it ---

    def add_stack(self, stack_id: int, name: str, endpoint_id: int = 1, *,
                  file: str | None = None, **extra) -> dict:
        stack = {"Id": stack_id, "Name": name, "EndpointId": endpoint_id,
                 "Type": 2, "Status": 1, "Env": [], **extra}
        self.stacks.append(stack)
        if file is not None:
            self.stack_files[stack_id] = file
        return stack

    def add_container(self, endpoint_id: int, name: str, image: str, *,
                      stack: str | None = None, state: str = "running",
                      status: str = "Up 2 hours", **extra) -> dict:
        cid = f"{len(self.calls) + sum(map(len, self.containers.values())) + 1:012x}"
        container = {
            "Id": extra.pop("Id", cid), "Names": [f"/{name}"], "Image": image,
            "ImageID": "sha256:" + "0" * 64, "State": state, "Status": status,
            "Labels": {"com.docker.compose.project": stack} if stack else {}, **extra,
        }
        self.containers.setdefault(endpoint_id, []).append(container)
        return container

    # --- answering as PortainerClient does ---

    def _answer(self, method: str, *args):
        self.calls.append((method, *args))
        for key in ((method, *args[:1]), method):
            if key in self.failing:
                raise self.failing[key]

    async def list_stacks(self):
        self._answer("list_stacks")
        return list(self.stacks)

    async def list_endpoints(self):
        self._answer("list_endpoints")
        return list(self.endpoints)

    async def list_containers(self, endpoint_id):
        self._answer("list_containers", endpoint_id)
        return list(self.containers[endpoint_id])

    async def get_stack(self, stack_id):
        self._answer("get_stack", stack_id)
        return next(s for s in self.stacks if s["Id"] == stack_id)

    async def get_stack_file(self, stack_id):
        self._answer("get_stack_file", stack_id)
        return self.stack_files[stack_id]

    async def get_container_info(self, endpoint_id, container_id):
        self._answer("get_container_info", endpoint_id, container_id)
        return self.container_info[container_id]

    async def get_image_info(self, endpoint_id, image):
        self._answer("get_image_info", endpoint_id, image)
        return self.image_info[image]

    async def reconnect(self):
        self.reconnected += 1

    async def aclose(self):
        pass
