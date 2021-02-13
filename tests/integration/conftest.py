"""
karapace - conftest

Copyright (c) 2019 Aiven Ltd
See LICENSE for details
"""
from dataclasses import dataclass
from filelock import FileLock
from kafka import KafkaAdminClient, KafkaProducer
from karapace.avro_compatibility import SchemaCompatibilityResult
from karapace.config import set_config_defaults, write_config
from karapace.kafka_rest_apis import KafkaRest, KafkaRestAdminClient
from karapace.schema_registry_apis import KarapaceSchemaRegistry
from pathlib import Path
from subprocess import Popen
from tests.utils import Client, client_for, KafkaConfig, new_random_name
from typing import AsyncIterator, Dict, Iterator, List, Optional, Tuple

import os
import pickle
import pytest
import random
import signal
import socket
import time

KAFKA_CURRENT_VERSION = "2.4"
BASEDIR = "kafka_2.12-2.4.1"
CLASSPATH = os.path.join(BASEDIR, "libs", "*")


@dataclass(frozen=True)
class PortRangeInclusive:
    start: int
    end: int

    PRIVILEGE_END = 2 ** 10
    MAX_PORTS = 2 ** 16 - 1

    def __post_init__(self):
        # Make sure the range is valid and that we don't need to be root
        assert self.end > self.start, "there must be at least one port available"
        assert self.end <= self.MAX_PORTS, f"end must be lower than {self.MAX_PORTS}"
        assert self.start > self.PRIVILEGE_END, "start must not be a privileged port"

    def next_range(self, number_of_ports: int) -> "PortRangeInclusive":
        next_start = self.end + 1
        next_end = next_start + number_of_ports - 1  # -1 because the range is inclusive

        return PortRangeInclusive(next_start, next_end)


# To find a good port range use the following:
#
#   curl --silent 'https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.txt' | \
#       egrep -i -e '^\s*[0-9]+-[0-9]+\s*unassigned' | \
#       awk '{print $1}'
#
KAFKA_PORTS = PortRangeInclusive(48700, 48800)
ZK_PORT_RANGE = KAFKA_PORTS.next_range(100)
REGISTRY_PORT_RANGE = ZK_PORT_RANGE.next_range(100)


class Timeout(Exception):
    pass


@dataclass(frozen=True)
class Expiration:
    msg: str
    deadline: float

    @classmethod
    def from_timeout(cls, msg: str, timeout: float):
        return cls(msg, time.monotonic() + timeout)

    def raise_if_expired(self):
        if time.monotonic() > self.deadline:
            raise Timeout(self.msg)


@dataclass
class ZKConfig:
    host: str
    client_port: int
    admin_port: int


def pytest_assertrepr_compare(op, left, right) -> Optional[List[str]]:
    if isinstance(left, SchemaCompatibilityResult) and isinstance(right, SchemaCompatibilityResult) and op in ("==", "!="):
        lines = ["Comparing SchemaCompatibilityResult instances:"]

        def pad(depth: int, *msg: str) -> str:
            return "  " * depth + ' '.join(msg)

        def list_details(header: str, depth: int, items: List[str]) -> None:
            qty = len(items)

            if qty == 1:
                lines.append(pad(depth, header, *items))
            elif qty > 1:
                lines.append(pad(depth, header))
                depth += 1
                for loc in items:
                    lines.append(pad(depth, loc))

        def compatibility_details(header: str, depth: int, obj: SchemaCompatibilityResult) -> None:
            lines.append(pad(depth, header))

            depth += 1

            lines.append(pad(depth, 'compatibility', str(obj.compatibility)))
            list_details('locations:', depth, list(obj.locations))
            list_details('messages:', depth, list(obj.messages))
            list_details('incompatibilities:', depth, [str(i) for i in obj.incompatibilities])

        depth = 1
        compatibility_details("Left:", depth, left)
        compatibility_details("Right:", depth, right)
        return lines

    return None


def pytest_addoption(parser, pluginmanager) -> None:  # pylint: disable=unused-argument
    parser.addoption("--zookeeper-dsn", help="host=<str>;client_port=<int>;admin_port=<int>")
    parser.addoption("--kafka-dsn", help="broker=<str>")


def port_is_listening(hostname: str, port: int, ipv6: bool) -> bool:
    if ipv6:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM, 0)
    else:
        s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((hostname, port))
        s.close()
        return True
    except socket.error:
        return False


def wait_for_kafka(broker: str, *, wait_time: float = 20.0) -> None:
    expiration = Expiration.from_timeout(
        msg=f"Could not contact kafka cluster on host `{broker}`",
        timeout=wait_time,
    )

    list_topics_successful = False
    while not list_topics_successful:
        expiration.raise_if_expired()
        try:
            KafkaAdminClient(bootstrap_servers=[broker]).list_topics()
        except Exception as e:  # pylint: disable=broad-except
            print(f"Error checking kafka cluster: {e}")
            time.sleep(2.0)
        else:
            list_topics_successful = True


def wait_for_port(port: int, *, hostname: str = "127.0.0.1", wait_time: float = 20.0, ipv6: bool = False) -> None:
    start_time = time.monotonic()
    expiration = Expiration(
        msg=f"Timeout waiting for `{hostname}:{port}`",
        deadline=start_time + wait_time,
    )

    while not port_is_listening(hostname, port, ipv6):
        expiration.raise_if_expired()
        time.sleep(2.0)

    elapsed = time.monotonic() - start_time
    print(f"Server `{hostname}:{port}` listening after {elapsed} seconds")


def get_random_port(*, port_range: PortRangeInclusive, blacklist: List[int]) -> int:
    """ Find a random port in the range `PortRangeInclusive`.

    Note:
        This function is *not* aware of the ports currently open in the system,
        the blacklist only prevents two services of the same type to randomly
        get the same ports for *a single test run*.

        Because of that, the port range should be chosen such that there is no
        system service in the range. Also note that running two sessions of the
        tests with the same range is not supported and will lead to flakiness.
    """
    value = random.randint(port_range.start, port_range.end)
    while value in blacklist:
        value = random.randint(port_range.start, port_range.end)
    return value


def lock_path_for(path: Path) -> Path:
    """ Append .lock to path """
    suffixes = path.suffixes
    suffixes.append('.lock')
    return path.with_suffix(''.join(suffixes))


def dsn_to_dict(dsn: str) -> dict:
    """ Converts a DSN string into a dictionary

    >>> dsn_to_dict('')
    dict()
    >>> dsn_to_dict('key=value')
    {'key': 'value'}
    >>> dsn_to_dict('key=value1;key=value2')
    ValueError(...)
    >>> dsn_to_dict('k1=v;k2=v;k3=v3')
    {'k1': 'v', 'k2': 'v', 'k3': 'v3'}
    """
    keys_and_values = (kv.split('=', maxsplit=1) for kv in dsn.split(';'))
    result = dict()
    for key, value in keys_and_values:
        if key in result:
            raise ValueError(f'Duplicated key {key}')
        result[key] = value
    return result


def dsn_to_multidict(dsn: str) -> dict:
    """ Converts a DSN string into a dictionary

    >>> dsn_to_dict('')
    dict()
    >>> dsn_to_dict('key=value')
    {'key': ['value']}
    >>> dsn_to_dict('key=value1;key=value2')
    {'key': ['value1', 'value2']}
    >>> dsn_to_dict('k1=v;k2=v;k3=v3')
    {'k1': ['v'], 'k2': ['v'], 'k3': ['v3']}
    """
    keys_and_values = (kv.split('=', maxsplit=1) for kv in dsn.split(';'))
    result = dict()
    for key, value in keys_and_values:
        result.setdefault(key, list()).append(value)
    return result


def zookeeper_dsn_to_config(zookeeper_dsn: str) -> ZKConfig:
    dsn = dsn_to_dict(zookeeper_dsn)
    return ZKConfig(
        host=dsn['host'],
        client_port=int(dsn['client_port']),
        admin_port=int(dsn['admin_port']),
    )


def kafka_dsn_to_config(kafka_dsn: str) -> KafkaConfig:
    dsn = dsn_to_multidict(kafka_dsn)
    return KafkaConfig(host=dsn['broker'][0])


@pytest.fixture(scope="session", name="zkserver")
def fixture_zkserver(session_tmppath: Path, pytestconfig) -> Iterator[ZKConfig]:
    zookeeper_dsn = pytestconfig.getoption('zookeeper_dsn')
    if zookeeper_dsn:
        yield zookeeper_dsn_to_config(zookeeper_dsn)
        return

    zk_dir = session_tmppath / "zk"
    transfer_file = session_tmppath / "zk_config"

    proc = None

    # Synchronize xdist workers, data generated by the winner is shared through
    # transfer_file (primarily the server's port number)
    with FileLock(str(lock_path_for(zk_dir))):
        if transfer_file.exists():
            config = pickle.loads(transfer_file.read_bytes())
        else:
            config, proc = configure_and_start_zk(zk_dir)
            transfer_file.write_bytes(pickle.dumps(config))
    try:
        wait_for_port(config.client_port)
        yield config
    finally:
        if proc:
            time.sleep(5)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10.0)


@pytest.fixture(scope="session", name="kafka_config")
def fixture_kafka_config(session_tmppath: Path, zkserver: ZKConfig, pytestconfig) -> Iterator[KafkaConfig]:
    kafka_dsn = pytestconfig.getoption('kafka_dsn')
    if kafka_dsn:
        yield kafka_dsn_to_config(kafka_dsn)
        return

    kafka_dir = session_tmppath / "kafka"
    transfer_file = session_tmppath / "kafka_config"

    proc = None

    # Synchronize xdist workers, data generated by the winner is shared through
    # transfer_file (primarily the server's port number)
    with FileLock(str(lock_path_for(kafka_dir))):
        if transfer_file.exists():
            config = pickle.loads(transfer_file.read_bytes())
        else:
            config, proc = configure_and_start_kafka(kafka_dir, zkserver)
            transfer_file.write_bytes(pickle.dumps(config))

    try:
        wait_for_kafka(config.broker, wait_time=60)
        yield config
    finally:
        if proc is not None:
            time.sleep(5)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10.0)


@pytest.fixture(scope="function", name="producer")
def fixture_producer(kafka_config: KafkaConfig) -> KafkaProducer:
    prod = KafkaProducer(bootstrap_servers=[kafka_config.broker])
    try:
        yield prod
    finally:
        prod.close()


@pytest.fixture(scope="function", name="admin_client")
def fixture_admin(kafka_config: KafkaConfig) -> Iterator[KafkaRestAdminClient]:
    cli = KafkaRestAdminClient(bootstrap_servers=[kafka_config.broker])
    try:
        yield cli
    finally:
        cli.close()


@pytest.fixture(scope="function", name="rest_async")
async def fixture_rest_async(tmp_path: Path, kafka_config: KafkaConfig,
                             registry_async_client: Client) -> AsyncIterator[KafkaRest]:
    config_path = tmp_path / "karapace_config.json"

    config = set_config_defaults({"log_level": "WARNING", "bootstrap_uri": kafka_config.broker, "admin_metadata_max_age": 0})
    write_config(config_path, config)
    rest = KafkaRest(config_file_path=str(config_path), config=config)

    assert rest.serializer.registry_client
    assert rest.consumer_manager.deserializer.registry_client
    rest.serializer.registry_client.client = registry_async_client
    rest.consumer_manager.deserializer.registry_client.client = registry_async_client
    try:
        yield rest
    finally:
        rest.close()
        await rest.close_producers()


@pytest.fixture(scope="function", name="rest_async_client")
async def fixture_rest_async_client(rest_async: KafkaRest, aiohttp_client) -> AsyncIterator[Client]:
    cli = await client_for(rest_async, aiohttp_client)
    yield cli
    await cli.close()


@pytest.fixture(scope="function", name="registry_async_pair")
def fixture_registry_async_pair(tmp_path: Path, kafka_config: KafkaConfig):
    master_config_path = tmp_path / "karapace_config_master.json"
    slave_config_path = tmp_path / "karapace_config_slave.json"
    master_port = get_random_port(port_range=REGISTRY_PORT_RANGE, blacklist=[])
    slave_port = get_random_port(port_range=REGISTRY_PORT_RANGE, blacklist=[master_port])
    topic_name = new_random_name("schema_pairs")
    group_id = new_random_name("schema_pairs")
    write_config(
        master_config_path, {
            "log_level": "WARNING",
            "bootstrap_uri": kafka_config.broker,
            "topic_name": topic_name,
            "group_id": group_id,
            "advertised_hostname": "127.0.0.1",
            "karapace_registry": True,
            "port": master_port,
        }
    )
    write_config(
        slave_config_path, {
            "log_level": "WARNING",
            "bootstrap_uri": kafka_config.broker,
            "topic_name": topic_name,
            "group_id": group_id,
            "advertised_hostname": "127.0.0.1",
            "karapace_registry": True,
            "port": slave_port,
        }
    )
    master_process = Popen(["python", "-m", "karapace.karapace_all", str(master_config_path)])
    slave_process = Popen(["python", "-m", "karapace.karapace_all", str(slave_config_path)])
    try:
        wait_for_port(master_port)
        wait_for_port(slave_port)
        yield f"http://127.0.0.1:{master_port}", f"http://127.0.0.1:{slave_port}"
    finally:
        master_process.kill()
        slave_process.kill()


@pytest.fixture(scope="function", name="registry_async")
async def fixture_registry_async(tmp_path: Path, kafka_config: KafkaConfig) -> AsyncIterator[KarapaceSchemaRegistry]:
    config_path = tmp_path / "karapace_config.json"

    config = set_config_defaults({
        "log_level": "WARNING",
        "bootstrap_uri": kafka_config.broker,
        "topic_name": new_random_name(),
        "group_id": new_random_name("schema_registry")
    })
    write_config(config_path, config)
    registry = KarapaceSchemaRegistry(config_file_path=str(config_path), config=set_config_defaults(config))
    await registry.get_master()
    try:
        yield registry
    finally:
        registry.close()


@pytest.fixture(scope="function", name="registry_async_client")
async def fixture_registry_async_client(registry_async: KarapaceSchemaRegistry, aiohttp_client) -> AsyncIterator[Client]:
    cli = await client_for(registry_async, aiohttp_client)
    yield cli
    await cli.close()


def zk_java_args(cfg_path: Path) -> List[str]:
    if not os.path.exists(BASEDIR):
        raise RuntimeError(
            f"Couldn't find kafka installation to run integration tests. The "
            f"expected folder {BASEDIR} does not exist. Run `make fetch-kafka` "
            f"to download and extract the release."
        )
    java_args = [
        "--class-path",
        CLASSPATH,
        "org.apache.zookeeper.server.quorum.QuorumPeerMain",
        str(cfg_path),
    ]
    return java_args


def kafka_java_args(heap_mb, kafka_config_path, logs_dir, log4j_properties_path):
    if not os.path.exists(BASEDIR):
        raise RuntimeError(
            f"Couldn't find kafka installation to run integration tests. The "
            f"expected folder {BASEDIR} does not exist. Run `make fetch-kafka` "
            f"to download and extract the release."
        )
    java_args = [
        "-Xmx{}M".format(heap_mb),
        "-Xms{}M".format(heap_mb),
        "-Dkafka.logs.dir={}/logs".format(logs_dir),
        "-Dlog4j.configuration=file:{}".format(log4j_properties_path),
        "--class-path",
        CLASSPATH,
        "kafka.Kafka",
        kafka_config_path,
    ]
    return java_args


def get_java_process_configuration(java_args: List[str]) -> List[str]:
    command = [
        "/usr/bin/java",
        "-server",
        "-XX:+UseG1GC",
        "-XX:MaxGCPauseMillis=20",
        "-XX:InitiatingHeapOccupancyPercent=35",
        "-XX:+DisableExplicitGC",
        "-XX:+ExitOnOutOfMemoryError",
        "-Djava.awt.headless=true",
        "-Dcom.sun.management.jmxremote",
        "-Dcom.sun.management.jmxremote.authenticate=false",
        "-Dcom.sun.management.jmxremote.ssl=false",
    ]
    command.extend(java_args)
    return command


def configure_and_start_kafka(kafka_dir: Path, zk: ZKConfig) -> Tuple[KafkaConfig, Popen]:
    # setup filesystem
    data_dir = kafka_dir / "data"
    config_dir = kafka_dir / "config"
    config_path = config_dir / "server.properties"
    data_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    host = '127.0.0.1'
    port = get_random_port(port_range=KAFKA_PORTS, blacklist=[])
    broker = f'{host}:{port}'

    config = KafkaConfig(broker=broker)

    advertised_listeners = ",".join([
        f"PLAINTEXT://{host}:{port}",
    ])
    listeners = ",".join([
        f"PLAINTEXT://:{port}",
    ])

    kafka_config = {
        "broker.id": 1,
        "broker.rack": "local",
        "advertised.listeners": advertised_listeners,
        "auto.create.topics.enable": False,
        "default.replication.factor": 1,
        "delete.topic.enable": "true",
        "inter.broker.listener.name": "PLAINTEXT",
        "inter.broker.protocol.version": KAFKA_CURRENT_VERSION,
        "listeners": listeners,
        "log.cleaner.enable": "true",
        "log.dirs": str(data_dir),
        "log.message.format.version": KAFKA_CURRENT_VERSION,
        "log.retention.check.interval.ms": 300000,
        "log.segment.bytes": 200 * 1024 * 1024,  # 200 MiB
        "num.io.threads": 8,
        "num.network.threads": 112,
        "num.partitions": 1,
        "num.replica.fetchers": 4,
        "num.recovery.threads.per.data.dir": 1,
        "offsets.topic.replication.factor": 1,
        "socket.receive.buffer.bytes": 100 * 1024,
        "socket.request.max.bytes": 100 * 1024 * 1024,
        "socket.send.buffer.bytes": 100 * 1024,
        "transaction.state.log.min.isr": 1,
        "transaction.state.log.num.partitions": 16,
        "transaction.state.log.replication.factor": 1,
        "zookeeper.connection.timeout.ms": 6000,
        "zookeeper.connect": f"{zk.host}:{zk.client_port}",
    }

    with config_path.open("w") as fp:
        for key, value in kafka_config.items():
            fp.write("{}={}\n".format(key, value))

    log4j_properties_path = os.path.join(BASEDIR, "config/log4j.properties")

    kafka_cmd = get_java_process_configuration(
        java_args=kafka_java_args(
            heap_mb=256,
            logs_dir=str(kafka_dir),
            log4j_properties_path=log4j_properties_path,
            kafka_config_path=str(config_path),
        ),
    )
    env: Dict[bytes, bytes] = dict()
    proc = Popen(kafka_cmd, env=env)
    return config, proc


def configure_and_start_zk(zk_dir: Path) -> Tuple[ZKConfig, Popen]:
    cfg_path = zk_dir / "zoo.cfg"
    logs_dir = zk_dir / "logs"
    logs_dir.mkdir(parents=True)

    client_port = get_random_port(port_range=ZK_PORT_RANGE, blacklist=[])
    admin_port = get_random_port(port_range=ZK_PORT_RANGE, blacklist=[client_port])
    config = ZKConfig(
        host='127.0.0.1',
        client_port=client_port,
        admin_port=admin_port,
    )
    zoo_cfg = """
# The number of milliseconds of each tick
tickTime=2000
# The number of ticks that the initial
# synchronization phase can take
initLimit=10
# The number of ticks that can pass between
# sending a request and getting an acknowledgement
syncLimit=5
# the directory where the snapshot is stored.
# do not use /tmp for storage, /tmp here is just
# example sakes.
dataDir={path}
# the port at which the clients will connect
clientPort={client_port}
#clientPortAddress=127.0.0.1
# the maximum number of client connections.
# increase this if you need to handle more clients
#maxClientCnxns=60
#
# Be sure to read the maintenance section of the
# administrator guide before turning on autopurge.
#
# http://zookeeper.apache.org/doc/current/zookeeperAdmin.html#sc_maintenance
#
# The number of snapshots to retain in dataDir
#autopurge.snapRetainCount=3
# Purge task interval in hours
# Set to "0" to disable auto purge feature
#autopurge.purgeInterval=1
# admin server
admin.serverPort={admin_port}
admin.enableServer=false
# Allow reconfig calls to be made to add/remove nodes to the cluster on the fly
reconfigEnabled=true
# Don't require authentication for reconfig
skipACL=yes
""".format(
        client_port=config.client_port,
        admin_port=config.admin_port,
        path=str(zk_dir),
    )
    cfg_path.write_text(zoo_cfg)
    env = {
        "CLASSPATH": "/usr/share/java/slf4j/slf4j-simple.jar",
        "ZOO_LOG_DIR": str(logs_dir),
    }
    java_args = get_java_process_configuration(java_args=zk_java_args(cfg_path))
    proc = Popen(java_args, env=env)
    return config, proc
