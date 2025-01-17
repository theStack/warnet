import logging
import re
import shutil
import subprocess
import yaml

from datetime import datetime
from pathlib import Path
from typing import cast, List, Tuple
import docker
from docker.models.containers import Container

from .interfaces import ContainerInterface
from warnet.utils import bubble_exception_str, parse_raw_messages
from services import SERVICES
from services.cadvisor import CAdvisor
from services.fork_observer import ForkObserver
from services.grafana import Grafana
from services.prometheus import Prometheus
from templates import TEMPLATES
from warnet.tank import Tank, CONTAINER_PREFIX_BITCOIND
from warnet.lnnode import LNNode
from warnet.utils import bubble_exception_str, parse_raw_messages, default_bitcoin_conf_args, set_execute_permission


DOCKER_COMPOSE_NAME = "docker-compose.yml"
DOCKERFILE_NAME = "Dockerfile"
TORRC_NAME = "torrc"
ENTRYPOINT_NAME = "entrypoint.sh"
DOCKER_REGISTRY = "bitcoindevproject/bitcoin-core"
GRAFANA_PROVISIONING = "grafana-provisioning"

logger = logging.getLogger("docker-interface")
logging.getLogger("docker.utils.config").setLevel(logging.WARNING)
logging.getLogger("docker.auth").setLevel(logging.WARNING)


class DockerInterface(ContainerInterface):
    def __init__(self, config_dir: Path) -> None:
        super().__init__(config_dir)
        self.client = docker.DockerClient = docker.from_env()

    @bubble_exception_str
    def build(self) -> bool:
        command = ["docker", "compose", "build"]
        try:
            with subprocess.Popen(
                command,
                cwd=str(self.config_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as process:
                logger.debug(f"Running docker compose build with PID {process.pid}")
                if process.stdout:
                    for line in process.stdout:
                        logger.info(line.decode().rstrip())
        except Exception as e:
            logger.error(
                f"An error occurred while executing `{' '.join(command)}` in {self.config_dir}: {e}"
            )
            return False
        return True


    @bubble_exception_str
    def up(self):
        command = ["docker", "compose", "up", "--detach"]
        try:
            with subprocess.Popen(
                command,
                cwd=str(self.config_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as process:
                logger.debug(f"Running docker compose up --detach with PID {process.pid}")
                if process.stdout:
                    for line in process.stdout:
                        logger.info(line.decode().rstrip())
        except Exception as e:
            logger.error(
                f"An error occurred while executing `{' '.join(command)}` in {self.config_dir}: {e}"
            )

    @bubble_exception_str
    def down(self):
        command = ["docker", "compose", "down"]
        try:
            with subprocess.Popen(
                command,
                cwd=str(self.config_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as process:
                logger.debug(f"Running docker compose down with PID {process.pid}")
                if process.stdout:
                    for line in process.stdout:
                        logger.info(line.decode().rstrip())
        except Exception as e:
            logger.error(
                f"An error occurred while executing `{' '.join(command)}` in {self.config_dir}: {e}"
            )

    def get_container(self, container_name: str) -> Container:
        return cast(Container, self.client.containers.get(container_name))

    def exec_run(self, container_name: str, cmd: str, user: str = "root"):
        c = self.get_container(container_name)
        result = c.exec_run(cmd=cmd, user=user)
        if result.exit_code != 0:
            raise Exception(
                f"Command failed with exit code {result.exit_code}: {result.output.decode('utf-8')}"
            )
        return result.output.decode("utf-8")

    def get_bitcoin_debug_log(self, container_name: str):
        now = datetime.utcnow()

        logs = self.client.api.logs(
            container=container_name,
            stdout=True,
            stderr=True,
            stream=False, # return a string
            until=now,
        )
        return cast(bytes, logs).decode('utf8') # cast for typechecker

    def get_bitcoin_cli(self, tank: Tank, method: str, params=None):
        if params:
            cmd = f"bitcoin-cli -regtest -rpcuser={tank.rpc_user} -rpcport={tank.rpc_port} -rpcpassword={tank.rpc_password} {method} {' '.join(map(str, params))}"
        else:
            cmd = f"bitcoin-cli -regtest -rpcuser={tank.rpc_user} -rpcport={tank.rpc_port} -rpcpassword={tank.rpc_password} {method}"
        return self.exec_run(tank.container_name, cmd, user="bitcoin")

    def get_file(self, container_name, file_path):
        container = self.get_container(container_name)
        data, stat = container.get_archive(file_path)
        out = b""
        for chunk in data:
            out += chunk
        # slice off tar archive header
        out = out[512:]
        # slice off end padding
        out = out[: stat["size"]]
        return out

    def get_messages(self, a_name: str, b_ipv4: str, bitcoin_network: str = "regtest"):
        # start with the IP of the peer
        # find the corresponding message capture folder
        # (which may include the internal port if connection is inbound)
        subdir = (
            "/" if bitcoin_network == "main" else f"{bitcoin_network}/"
        )
        dirs = self.exec_run(a_name, f"ls /home/bitcoin/.bitcoin/{subdir}message_capture")
        dirs = dirs.splitlines()
        messages = []
        for dir_name in dirs:
            if b_ipv4 in dir_name:
                for file, outbound in [["msgs_recv.dat", False], ["msgs_sent.dat", True]]:
                    blob = self.get_file(
                        a_name,
                        f"/home/bitcoin/.bitcoin/{subdir}message_capture/{dir_name}/{file}")
                    json = parse_raw_messages(blob, outbound)
                    messages = messages + json
        messages.sort(key=lambda x: x["time"])
        return messages

    def get_containers_in_network(self, network: str) -> List[str]:
        # Return list of container names in the specified network
        containers = []
        for container in self.client.containers.list(filters={"network": network}):
            containers.append(container.name)
        logger.debug(f"Got containers: {containers}")
        return containers

    def logs_grep(self, pattern: str, network: str) -> str:
        compiled_pattern = re.compile(pattern)
        containers = self.get_containers_in_network(network)

        all_matching_logs: List[Tuple[str, str]] = []

        for container_name in containers:
            logger.debug(f"Fetching logs from {container_name}")
            logs = self.client.api.logs(
                container=container_name,
                stdout=True,
                stderr=True,
                stream=False,
            )
            logs = logs.decode("utf-8").splitlines()

            for log_entry in logs:
                log_entry_str = log_entry.strip()
                if compiled_pattern.search(log_entry_str):
                    all_matching_logs.append((container_name, log_entry_str))

        # Sort by timestamp; Python's default tuple sorting will sort by the second element, which is the timestamp
        all_matching_logs.sort(key=lambda x: x[1])

        # Format and join the sorted logs
        sorted_logs = [f"{container} {log}" for container, log in all_matching_logs]

        return '\n'.join(sorted_logs)

    def write_prometheus_config(self, warnet):
        config = {
            "global": {"scrape_interval": "15s"},
            "scrape_configs": [
                {
                    "job_name": "cadvisor",
                    "scrape_interval": "15s",
                    "static_configs": [{"targets": [f"{warnet.network_name}_cadvisor:8080"]}],
                },
            ],
        }

        prometheus_path = self.config_dir / "prometheus.yml"
        try:
            with open(prometheus_path, "w") as file:
                yaml.dump(config, file)
            logger.info(f"Wrote file: {prometheus_path}")
        except Exception as e:
            logger.error(f"An error occurred while writing to {prometheus_path}: {e}")

    def _write_docker_compose(self, warnet):
        compose = {
            "version": "3.8",
            "networks": {
                warnet.network_name: {
                    "name": warnet.network_name,
                    "ipam": {"config": [{"subnet": warnet.subnet}]},
                }
            },
            "volumes": {"grafana-storage": None},
            "services": {},
        }

        # Pass services object to each tank so they can add whatever they need.
        for tank in warnet.tanks:
            self.add_services(tank, compose["services"])

        # Initialize services and add them to the compose
        services = [
            # grep: disable-exporters
            Prometheus(warnet.network_name, self.config_dir),
            # NodeExporter(warnet.network_name),
            Grafana(warnet.network_name),
            CAdvisor(warnet.network_name, TEMPLATES),
            ForkObserver(warnet.network_name, warnet.fork_observer_config),
            # Fluentd(warnet.network_name, warnet.config_dir),
        ]

        for service_obj in services:
            service_name = service_obj.__class__.__name__.lower()
            compose["services"][service_name] = service_obj.get_service()

        docker_compose_path = warnet.config_dir / "docker-compose.yml"
        try:
            with open(docker_compose_path, "w") as file:
                yaml.dump(compose, file)
            logger.info(f"Wrote file: {docker_compose_path}")
        except Exception as e:
            logger.error(
                f"An error occurred while writing to {docker_compose_path}: {e}"
            )

    def generate_deployment_file(self, warnet):
        self._write_docker_compose(warnet)
        self.write_prometheus_config(warnet)
        warnet.deployment_file = warnet.config_dir / DOCKER_COMPOSE_NAME
        logger.debug(f"{SERVICES=}")
        logger.debug(f"{SERVICES / GRAFANA_PROVISIONING=}")
        logger.debug(f"{self.config_dir=}")
        shutil.copytree(SERVICES / GRAFANA_PROVISIONING, self.config_dir / GRAFANA_PROVISIONING, dirs_exist_ok=True)


    def default_config_args(self, tank):
        defaults = default_bitcoin_conf_args()
        defaults += f" -rpcuser={tank.rpc_user}"
        defaults += f" -rpcpassword={tank.rpc_password}"
        defaults += f" -rpcport={tank.rpc_port}"
        defaults += f" -zmqpubrawblock=tcp://0.0.0.0:{tank.zmqblockport}"
        defaults += f" -zmqpubrawtx=tcp://0.0.0.0:{tank.zmqtxport}"
        return defaults

    def copy_configs(self, tank):
        shutil.copyfile(TEMPLATES / DOCKERFILE_NAME, tank.config_dir / DOCKERFILE_NAME)
        shutil.copyfile(TEMPLATES / TORRC_NAME, tank.config_dir / TORRC_NAME)
        shutil.copyfile(TEMPLATES / ENTRYPOINT_NAME, tank.config_dir / ENTRYPOINT_NAME)
        set_execute_permission(tank.config_dir / ENTRYPOINT_NAME)

    def add_services(self, tank, services):
        assert tank.index is not None
        services[tank.container_name] = {}
        logger.debug(f"{tank.version=}")

        # Setup bitcoind, either release binary or build from source
        if "/" and "#" in tank.version:
            # it's a git branch, building step is necessary
            repo, branch = tank.version.split("#")
            build = {
                "context": str(tank.config_dir),
                "dockerfile": str(tank.config_dir / DOCKERFILE_NAME),
                "args": {
                    "REPO": repo,
                    "BRANCH": branch,
                    "BUILD_ARGS": f"{tank.DEFAULT_BUILD_ARGS + tank.extra_build_args}",
                },
            }
            services[tank.container_name]['build'] = build
            self.copy_configs(tank)
        else:
            image = f"{DOCKER_REGISTRY}:{tank.version}"
            services[tank.container_name]['image'] = image
        # Add common bitcoind service details
        services[tank.container_name].update(
            {
                "container_name": tank.container_name,
                "environment": {
                    "BITCOIN_ARGS": self.default_config_args(tank)
                },
                "networks": {
                    tank.network_name: {
                        "ipv4_address": f"{tank.ipv4}",
                    }
                },
                "labels": {"warnet": "tank"},
                "privileged": True,
                "cap_add": ["NET_ADMIN", "NET_RAW"],
                "healthcheck": {
                    "test": ["CMD", "pidof", "bitcoind"],
                    "interval": "10s",           # Check every 10 seconds
                    "timeout": "1s",             # Give the check 1 second to complete
                    "start_period": "5s",        # Start checking after 5 seconds
                    "retries": 3
                },
            }
        )

        if tank.lnnode is not None:
            tank.lnnode.add_services(services)
            services[tank.container_name].update(
            {
                "labels": {
                    "lnnode_container_name": tank.lnnode.container_name,
                    "lnnode_ipv4_address": tank.lnnode.ipv4,
                    "lnnode_impl": tank.lnnode.impl
                }
            })

        # grep: disable-exporters
        # Add the prometheus data exporter in a neighboring container
        # services[self.exporter_name] = {
        #     "image": "jvstein/bitcoin-prometheus-exporter",
        #     "container_name": self.exporter_name,
        #     "environment": {
        #         "BITCOIN_RPC_HOST": self.container_name,
        #         "BITCOIN_RPC_PORT": self.rpc_port,
        #         "BITCOIN_RPC_USER": self.rpc_user,
        #         "BITCOIN_RPC_PASSWORD": self.rpc_password,
        #     },
        #     "ports": [f"{8335 + self.index}:9332"],
        #     "networks": [self.docker_network],
        # }

    # def add_scrapers(self, scrapers):
    #     scrapers.append(
    #         {
    #             "job_name": self.container_name,
    #             "scrape_interval": "5s",
    #             "static_configs": [{"targets": [f"{self.exporter_name}:9332"]}],
    #         }
    #     )

    def warnet_from_deployment(self, warnet):
        # Get tank names, versions and IP addresses from docker-compose
        docker_compose_path = warnet.config_dir / DOCKER_COMPOSE_NAME
        compose = None
        with open(docker_compose_path, "r") as file:
            compose = yaml.safe_load(file)
        for service_name in compose["services"]:
            tank = self.tank_from_deployment(compose["services"][service_name], warnet)
            if tank is not None:
                warnet.tanks.append(tank)

    def tank_from_deployment(self, service, warnet):
        rex = fr"{warnet.network_name}_{CONTAINER_PREFIX_BITCOIND}_([0-9]{{6}})"
        # Not a tank, maybe a scaled service
        if not "container_name" in service:
            return None
        match = re.match(rex, service["container_name"])
        if match is None:
            return None

        index = int(match.group(1))
        tank = Tank(index, warnet.config_dir, warnet)
        tank._ipv4 = service["networks"][tank.network_name]["ipv4_address"]
        if "image" in service:
            tank.version = service["image"].split(":")[1]
        else:
            tank.version = f"{service['build']['args']['REPO']}#{service['build']['args']['BRANCH']}"

        labels = service.get("labels", {})
        if "lnnode_impl" in labels:
            tank.lnnode = LNNode(warnet, tank, labels["lnnode_impl"])
            tank.lnnode.container_name = labels.get("lnnode_container_name")
            tank.lnnode.ipv4 = labels.get("lnnode_ipv4_address")
        return tank
