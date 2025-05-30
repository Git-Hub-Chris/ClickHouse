import io
import json
import logging
import random
import string
import time
import uuid
from multiprocessing.dummy import Pool

import pytest
from kazoo.exceptions import NoNodeError

from helpers.client import QueryRuntimeException
from helpers.cluster import ClickHouseCluster, ClickHouseInstance

AVAILABLE_MODES = ["unordered", "ordered"]
DEFAULT_AUTH = ["'minio'", "'minio123'"]
NO_AUTH = ["NOSIGN"]


def prepare_public_s3_bucket(started_cluster):
    def create_bucket(client, bucket_name, policy):
        if client.bucket_exists(bucket_name):
            client.remove_bucket(bucket_name)

        client.make_bucket(bucket_name)

        client.set_bucket_policy(bucket_name, json.dumps(policy))

    def get_policy_with_public_access(bucket_name):
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": [
                        "s3:GetBucketLocation",
                        "s3:ListBucket",
                    ],
                    "Resource": f"arn:aws:s3:::{bucket_name}",
                },
                {
                    "Sid": "",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                    ],
                    "Resource": f"arn:aws:s3:::{bucket_name}/*",
                },
            ],
        }

    minio_client = started_cluster.minio_client

    started_cluster.minio_public_bucket = f"{started_cluster.minio_bucket}-public"
    create_bucket(
        minio_client,
        started_cluster.minio_public_bucket,
        get_policy_with_public_access(started_cluster.minio_public_bucket),
    )


@pytest.fixture(autouse=True)
def s3_queue_setup_teardown(started_cluster):
    instance = started_cluster.instances["instance"]
    instance_2 = started_cluster.instances["instance2"]

    instance.query("DROP DATABASE IF EXISTS default; CREATE DATABASE default;")
    instance_2.query("DROP DATABASE IF EXISTS default; CREATE DATABASE default;")

    minio = started_cluster.minio_client
    objects = list(minio.list_objects(started_cluster.minio_bucket, recursive=True))
    for obj in objects:
        minio.remove_object(started_cluster.minio_bucket, obj.object_name)

    container_client = started_cluster.blob_service_client.get_container_client(
        started_cluster.azurite_container
    )

    if container_client.exists():
        blob_names = [b.name for b in container_client.list_blobs()]
        logging.debug(f"Deleting blobs: {blob_names}")
        for b in blob_names:
            container_client.delete_blob(b)

    yield  # run test


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster = ClickHouseCluster(__file__)
        cluster.add_instance(
            "instance",
            user_configs=["configs/users.xml"],
            with_minio=True,
            with_azurite=True,
            with_zookeeper=True,
            main_configs=[
                "configs/zookeeper.xml",
                "configs/s3queue_log.xml",
            ],
            stay_alive=True,
        )
        cluster.add_instance(
            "instance2",
            user_configs=["configs/users.xml"],
            with_minio=True,
            with_zookeeper=True,
            main_configs=[
                "configs/s3queue_log.xml",
            ],
            stay_alive=True,
        )
        cluster.add_instance(
            "old_instance",
            with_zookeeper=True,
            image="clickhouse/clickhouse-server",
            tag="23.12",
            stay_alive=True,
            with_installed_binary=True,
            use_old_analyzer=True,
        )
        cluster.add_instance(
            "node1",
            with_zookeeper=True,
            stay_alive=True,
            main_configs=[
                "configs/zookeeper.xml",
                "configs/s3queue_log.xml",
                "configs/remote_servers.xml",
            ],
        )
        cluster.add_instance(
            "node2",
            with_zookeeper=True,
            stay_alive=True,
            main_configs=[
                "configs/zookeeper.xml",
                "configs/s3queue_log.xml",
                "configs/remote_servers.xml",
            ],
        )
        cluster.add_instance(
            "instance_too_many_parts",
            user_configs=["configs/users.xml"],
            with_minio=True,
            with_zookeeper=True,
            main_configs=[
                "configs/s3queue_log.xml",
                "configs/merge_tree.xml",
            ],
            stay_alive=True,
        )
        cluster.add_instance(
            "instance_24.5",
            with_zookeeper=True,
            image="clickhouse/clickhouse-server",
            tag="24.5",
            stay_alive=True,
            user_configs=[
                "configs/users.xml",
            ],
            with_installed_binary=True,
        )
        cluster.add_instance(
            "instance2_24.5",
            with_zookeeper=True,
            keeper_required_feature_flags=["create_if_not_exists"],
            image="clickhouse/clickhouse-server",
            tag="24.5",
            stay_alive=True,
            user_configs=[
                "configs/users.xml",
            ],
            with_installed_binary=True,
        )
        cluster.add_instance(
            "instance3_24.5",
            with_zookeeper=True,
            image="clickhouse/clickhouse-server",
            tag="24.5",
            stay_alive=True,
            main_configs=[
                "configs/remote_servers_245.xml",
            ],
            user_configs=[
                "configs/users.xml",
            ],
            with_installed_binary=True,
        )
        cluster.add_instance(
            "instance4_24.5",
            with_zookeeper=True,
            keeper_required_feature_flags=["create_if_not_exists"],
            image="clickhouse/clickhouse-server",
            tag="24.5",
            stay_alive=True,
            main_configs=[
                "configs/remote_servers_245.xml",
            ],
            user_configs=[
                "configs/users.xml",
            ],
            with_installed_binary=True,
        )
        cluster.add_instance(
            "node_cloud_mode",
            with_zookeeper=True,
            stay_alive=True,
            main_configs=[
                "configs/zookeeper.xml",
                "configs/s3queue_log.xml",
            ],
            user_configs=["configs/cloud_mode.xml"],
        )

        logging.info("Starting cluster...")
        cluster.start()
        logging.info("Cluster started")

        yield cluster
    finally:
        cluster.shutdown()


def run_query(instance, query, stdin=None, settings=None):
    # type: (ClickHouseInstance, str, object, dict) -> str

    logging.info("Running query '{}'...".format(query))
    result = instance.query(query, stdin=stdin, settings=settings)
    logging.info("Query finished")

    return result


def random_str(length=6):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def generate_random_files(
    started_cluster,
    files_path,
    count,
    storage="s3",
    column_num=3,
    row_num=10,
    start_ind=0,
    bucket=None,
    use_prefix=None,
    use_random_names=False,
    files=None,
):
    if files is not None:
        pass
    elif use_random_names:
        files = [
            (f"{files_path}/{random_str(10)}.csv", i)
            for i in range(start_ind, start_ind + count)
        ]
    elif use_prefix is not None:
        files = [
            (f"{files_path}/{use_prefix}_{i}.csv", i)
            for i in range(start_ind, start_ind + count)
        ]
    else:
        files = [
            (f"{files_path}/test_{i}.csv", i)
            for i in range(start_ind, start_ind + count)
        ]
    files.sort(key=lambda x: x[0])

    print(f"Generating files: {files}")

    total_values = []
    for filename, i in files:
        rand_values = [
            [random.randint(0, 1000) for _ in range(column_num)] for _ in range(row_num)
        ]
        total_values += rand_values
        values_csv = (
            "\n".join((",".join(map(str, row)) for row in rand_values)) + "\n"
        ).encode()
        if storage == "s3":
            put_s3_file_content(started_cluster, filename, values_csv, bucket)
        else:
            put_azure_file_content(started_cluster, filename, values_csv, bucket)
    return total_values


def put_s3_file_content(started_cluster, filename, data, bucket=None):
    bucket = started_cluster.minio_bucket if bucket is None else bucket
    buf = io.BytesIO(data)
    started_cluster.minio_client.put_object(bucket, filename, buf, len(data))


def put_azure_file_content(started_cluster, filename, data, bucket=None):
    client = started_cluster.blob_service_client.get_blob_client(
        started_cluster.azurite_container, filename
    )
    buf = io.BytesIO(data)
    client.upload_blob(buf, "BlockBlob", len(data))


def create_table(
    started_cluster,
    node,
    table_name,
    mode,
    files_path,
    engine_name="S3Queue",
    version=None,
    format="column1 UInt32, column2 UInt32, column3 UInt32",
    additional_settings={},
    file_format="CSV",
    auth=DEFAULT_AUTH,
    bucket=None,
    expect_error=False,
    database_name="default",
    no_settings=False,
):
    auth_params = ",".join(auth)
    bucket = started_cluster.minio_bucket if bucket is None else bucket

    settings = {
        "s3queue_loading_retries": 0,
        "after_processing": "keep",
        "keeper_path": f"/clickhouse/test_{table_name}",
        "mode": f"{mode}",
    }
    if version is None:
        settings["enable_hash_ring_filtering"] = 1

    settings.update(additional_settings)

    engine_def = None
    if engine_name == "S3Queue":
        url = f"http://{started_cluster.minio_host}:{started_cluster.minio_port}/{bucket}/{files_path}/"
        engine_def = f"{engine_name}('{url}', {auth_params}, {file_format})"
    else:
        engine_def = f"{engine_name}('{started_cluster.env_variables['AZURITE_CONNECTION_STRING']}', '{started_cluster.azurite_container}', '{files_path}/', 'CSV')"

    node.query(f"DROP TABLE IF EXISTS {table_name}")
    if no_settings:
        create_query = f"""
            CREATE TABLE {database_name}.{table_name} ({format})
            ENGINE = {engine_def}
            """
    else:
        create_query = f"""
            CREATE TABLE {database_name}.{table_name} ({format})
            ENGINE = {engine_def}
            SETTINGS {",".join((k+"="+repr(v) for k, v in settings.items()))}
            """

    if expect_error:
        return node.query_and_get_error(create_query)

    node.query(create_query)


def create_mv(
    node,
    src_table_name,
    dst_table_name,
    mv_name=None,
    format="column1 UInt32, column2 UInt32, column3 UInt32",
):
    if mv_name is None:
        mv_name = f"{src_table_name}_mv"
    node.query(
        f"""
        DROP TABLE IF EXISTS {dst_table_name};
        DROP TABLE IF EXISTS {mv_name};

        CREATE TABLE {dst_table_name} ({format}, _path String)
        ENGINE = MergeTree()
        ORDER BY column1;

        CREATE MATERIALIZED VIEW {mv_name} TO {dst_table_name} AS SELECT *, _path FROM {src_table_name};
        """
    )


def generate_random_string(length=6):
    return "".join(random.choice(string.ascii_lowercase) for i in range(length))


@pytest.mark.parametrize("mode", ["unordered", "ordered"])
@pytest.mark.parametrize("engine_name", ["S3Queue", "AzureQueue"])
def test_delete_after_processing(started_cluster, mode, engine_name):
    node = started_cluster.instances["instance"]
    table_name = (
        f"delete_after_processing_{mode}_{engine_name}_{generate_random_string()}"
    )
    dst_table_name = f"{table_name}_dst"
    files_path = f"{table_name}_data"
    files_num = 5
    row_num = 10
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    if engine_name == "S3Queue":
        storage = "s3"
    else:
        storage = "azure"

    total_values = generate_random_files(
        started_cluster, files_path, files_num, row_num=row_num, storage=storage
    )
    create_table(
        started_cluster,
        node,
        table_name,
        mode,
        files_path,
        additional_settings={"after_processing": "delete", "keeper_path": keeper_path},
        engine_name=engine_name,
    )
    create_mv(node, table_name, dst_table_name)

    expected_count = files_num * row_num
    for _ in range(100):
        count = int(node.query(f"SELECT count() FROM {dst_table_name}"))
        print(f"{count}/{expected_count}")
        if count == expected_count:
            break
        time.sleep(1)

    assert int(node.query(f"SELECT count() FROM {dst_table_name}")) == expected_count
    assert int(node.query(f"SELECT uniq(_path) FROM {dst_table_name}")) == files_num
    assert [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name} ORDER BY column1, column2, column3"
        ).splitlines()
    ] == sorted(total_values, key=lambda x: (x[0], x[1], x[2]))

    node.query("system flush logs")

    if engine_name == "S3Queue":
        system_tables = ["s3queue_log", "s3queue"]
    else:
        system_tables = ["azure_queue_log", "azure_queue"]

    for table in system_tables:
        if table.endswith("_log"):
            assert (
                int(
                    node.query(
                        f"SELECT sum(rows_processed) FROM system.{table} WHERE table = '{table_name}'"
                    )
                )
                == files_num * row_num
            )
        else:
            assert (
                int(
                    node.query(
                        f"SELECT sum(rows_processed) FROM system.{table} WHERE zookeeper_path = '{keeper_path}'"
                    )
                )
                == files_num * row_num
            )

    if engine_name == "S3Queue":
        minio = started_cluster.minio_client
        objects = list(minio.list_objects(started_cluster.minio_bucket, recursive=True))
        assert len(objects) == 0
    else:
        client = started_cluster.blob_service_client.get_container_client(
            started_cluster.azurite_container
        )
        objects_iterator = client.list_blobs(files_path)
        for objects in objects_iterator:
            assert False


@pytest.mark.parametrize("mode", ["unordered", "ordered"])
@pytest.mark.parametrize("engine_name", ["S3Queue", "AzureQueue"])
def test_failed_retry(started_cluster, mode, engine_name):
    node = started_cluster.instances["instance"]
    table_name = f"failed_retry_{mode}_{engine_name}"
    dst_table_name = f"{table_name}_dst"
    files_path = f"{table_name}_data"
    file_path = f"{files_path}/trash_test.csv"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    retries_num = 3

    values = [
        ["failed", 1, 1],
    ]
    values_csv = (
        "\n".join((",".join(map(str, row)) for row in values)) + "\n"
    ).encode()
    if engine_name == "S3Queue":
        put_s3_file_content(started_cluster, file_path, values_csv)
    else:
        put_azure_file_content(started_cluster, file_path, values_csv)

    create_table(
        started_cluster,
        node,
        table_name,
        mode,
        files_path,
        additional_settings={
            "s3queue_loading_retries": retries_num,
            "keeper_path": keeper_path,
            "polling_max_timeout_ms": 5000,
            "polling_backoff_ms": 1000,
        },
        engine_name=engine_name,
    )
    create_mv(node, table_name, dst_table_name)

    failed_node_path = ""
    for _ in range(20):
        zk = started_cluster.get_kazoo_client("zoo1")
        failed_nodes = zk.get_children(f"{keeper_path}/failed/")
        if len(failed_nodes) > 0:
            assert len(failed_nodes) == 1
            failed_node_path = f"{keeper_path}/failed/{failed_nodes[0]}"
        time.sleep(1)

    assert failed_node_path != ""

    retries = 0
    for _ in range(20):
        data, stat = zk.get(failed_node_path)
        json_data = json.loads(data)
        print(f"Failed node metadata: {json_data}")
        assert json_data["file_path"] == file_path
        retries = int(json_data["retries"])
        if retries == retries_num:
            break
        time.sleep(1)

    assert retries == retries_num
    assert 0 == int(node.query(f"SELECT count() FROM {dst_table_name}"))


@pytest.mark.parametrize("mode", AVAILABLE_MODES)
def test_direct_select_file(started_cluster, mode):
    node = started_cluster.instances["instance"]
    table_name = f"direct_select_file_{mode}"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{mode}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    file_path = f"{files_path}/test.csv"

    values = [
        [12549, 2463, 19893],
        [64021, 38652, 66703],
        [81611, 39650, 83516],
    ]
    values_csv = (
        "\n".join((",".join(map(str, row)) for row in values)) + "\n"
    ).encode()
    put_s3_file_content(started_cluster, file_path, values_csv)

    for i in range(3):
        create_table(
            started_cluster,
            node,
            f"{table_name}_{i + 1}",
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "s3queue_processing_threads_num": 1,
                "enable_hash_ring_filtering": 0,
            },
        )

    assert [
        list(map(int, l.split()))
        for l in node.query(f"SELECT * FROM {table_name}_1").splitlines()
    ] == values

    assert [
        list(map(int, l.split()))
        for l in node.query(f"SELECT * FROM {table_name}_2").splitlines()
    ] == []

    assert [
        list(map(int, l.split()))
        for l in node.query(f"SELECT * FROM {table_name}_3").splitlines()
    ] == []

    # New table with same zookeeper path
    create_table(
        started_cluster,
        node,
        f"{table_name}_4",
        mode,
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": 1,
        },
    )

    assert [
        list(map(int, l.split()))
        for l in node.query(f"SELECT * FROM {table_name}_4").splitlines()
    ] == []

    # New table with different zookeeper path
    keeper_path = f"{keeper_path}_2"
    create_table(
        started_cluster,
        node,
        f"{table_name}_4",
        mode,
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": 1,
        },
    )

    assert [
        list(map(int, l.split()))
        for l in node.query(f"SELECT * FROM {table_name}_4").splitlines()
    ] == values

    values = [
        [1, 1, 1],
    ]
    values_csv = (
        "\n".join((",".join(map(str, row)) for row in values)) + "\n"
    ).encode()
    file_path = f"{files_path}/t.csv"
    put_s3_file_content(started_cluster, file_path, values_csv)

    if mode == "unordered":
        assert [
            list(map(int, l.split()))
            for l in node.query(f"SELECT * FROM {table_name}_4").splitlines()
        ] == values
    elif mode == "ordered":
        assert [
            list(map(int, l.split()))
            for l in node.query(f"SELECT * FROM {table_name}_4").splitlines()
        ] == []


@pytest.mark.parametrize("mode", AVAILABLE_MODES)
def test_direct_select_multiple_files(started_cluster, mode):
    node = started_cluster.instances["instance"]
    table_name = f"direct_select_multiple_files_{mode}"
    files_path = f"{table_name}_data"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"

    create_table(
        started_cluster,
        node,
        table_name,
        mode,
        files_path,
        additional_settings={"keeper_path": keeper_path, "processing_threads_num": 3},
    )
    for i in range(5):
        rand_values = [[random.randint(0, 50) for _ in range(3)] for _ in range(10)]
        values_csv = (
            "\n".join((",".join(map(str, row)) for row in rand_values)) + "\n"
        ).encode()

        file_path = f"{files_path}/test_{i}.csv"
        put_s3_file_content(started_cluster, file_path, values_csv)

        assert [
            list(map(int, l.split()))
            for l in node.query(f"SELECT * FROM {table_name}").splitlines()
        ] == rand_values

    total_values = generate_random_files(started_cluster, files_path, 4, start_ind=5)
    assert {
        tuple(map(int, l.split()))
        for l in node.query(f"SELECT * FROM {table_name}").splitlines()
    } == set([tuple(i) for i in total_values])


@pytest.mark.parametrize("mode", AVAILABLE_MODES)
def test_streaming_to_view(started_cluster, mode):
    node = started_cluster.instances["instance"]
    table_name = f"streaming_to_view_{mode}"
    dst_table_name = f"{table_name}_dst"
    files_path = f"{table_name}_data"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"

    total_values = generate_random_files(started_cluster, files_path, 10)
    create_table(
        started_cluster,
        node,
        table_name,
        mode,
        files_path,
        additional_settings={"keeper_path": keeper_path},
    )
    create_mv(node, table_name, dst_table_name)

    expected_values = set([tuple(i) for i in total_values])
    for i in range(10):
        selected_values = {
            tuple(map(int, l.split()))
            for l in node.query(
                f"SELECT column1, column2, column3 FROM {dst_table_name}"
            ).splitlines()
        }
        if selected_values == expected_values:
            break
        time.sleep(1)
    assert selected_values == expected_values


@pytest.mark.parametrize("mode", AVAILABLE_MODES)
def test_streaming_to_many_views(started_cluster, mode):
    node = started_cluster.instances["instance"]
    table_name = f"streaming_to_many_views_{mode}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"

    for i in range(10):
        table = f"{table_name}_{i + 1}"
        create_table(
            started_cluster,
            node,
            table,
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "polling_min_timeout_ms": 100,
                "polling_max_timeout_ms": 100,
                "polling_backoff_ms": 0,
            },
        )
        create_mv(node, table, dst_table_name)

    files_num = 20
    row_num = 100
    files = [(f"{files_path}/test_{i}.csv", i) for i in range(0, files_num)]
    total_values = generate_random_files(
        started_cluster, files_path, files_num, files=files, row_num=row_num
    )
    expected_values = sorted(set([tuple(i) for i in total_values]))

    def check():
        return int(node.query(f"SELECT uniqExact(_path) FROM {dst_table_name}"))

    for _ in range(20):
        if check() == files_num:
            break
        time.sleep(1)
    processed_files = node.query(f"SELECT distinct(_path) FROM {dst_table_name}")
    missing_files = [
        x[0] for x in files if x[0] not in processed_files.strip().split("\n")
    ]
    assert check() == files_num, f"Missing files: {missing_files}"
    assert row_num * files_num == int(
        node.query(f"SELECT count() FROM {dst_table_name}")
    )


def test_multiple_tables_meta_mismatch(started_cluster):
    node = started_cluster.instances["instance"]
    table_name = f"multiple_tables_meta_mismatch"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
        },
    )
    # check mode
    failed = False
    try:
        create_table(
            started_cluster,
            node,
            f"{table_name}_copy",
            "unordered",
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
            },
        )
    except QueryRuntimeException as e:
        assert "Existing table metadata in ZooKeeper differs in engine mode" in str(e)
        failed = True

    assert failed is True

    # check columns
    try:
        create_table(
            started_cluster,
            node,
            f"{table_name}_copy",
            "ordered",
            files_path,
            format="column1 UInt32, column2 UInt32, column3 UInt32, column4 UInt32",
            additional_settings={
                "keeper_path": keeper_path,
            },
        )
    except QueryRuntimeException as e:
        assert "Existing table metadata in ZooKeeper differs in columns" in str(e)
        failed = True

    assert failed is True

    # check format
    try:
        create_table(
            started_cluster,
            node,
            f"{table_name}_copy",
            "ordered",
            files_path,
            format="column1 UInt32, column2 UInt32, column3 UInt32, column4 UInt32",
            additional_settings={
                "keeper_path": keeper_path,
            },
            file_format="TSV",
        )
    except QueryRuntimeException as e:
        assert "Existing table metadata in ZooKeeper differs in format name" in str(e)
        failed = True

    assert failed is True

    # create working engine
    create_table(
        started_cluster,
        node,
        f"{table_name}_copy",
        "ordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
        },
    )


# TODO: Update the modes for this test to include "ordered" once PR #55795 is finished.
@pytest.mark.parametrize("mode", ["unordered"])
def test_multiple_tables_streaming_sync(started_cluster, mode):
    node = started_cluster.instances["instance"]
    table_name = f"multiple_tables_streaming_sync_{mode}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 300

    for i in range(3):
        table = f"{table_name}_{i + 1}"
        dst_table = f"{dst_table_name}_{i + 1}"
        create_table(
            started_cluster,
            node,
            table,
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
            },
        )
        create_mv(node, table, dst_table)

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, row_num=1
    )

    def get_count(table_name):
        return int(run_query(node, f"SELECT count() FROM {table_name}"))

    for _ in range(100):
        if (
            get_count(f"{dst_table_name}_1")
            + get_count(f"{dst_table_name}_2")
            + get_count(f"{dst_table_name}_3")
        ) == files_to_generate:
            break
        time.sleep(1)

    if (
        get_count(f"{dst_table_name}_1")
        + get_count(f"{dst_table_name}_2")
        + get_count(f"{dst_table_name}_3")
    ) != files_to_generate:
        info = node.query(
            f"SELECT * FROM system.s3queue WHERE zookeeper_path like '%{table_name}' ORDER BY file_name FORMAT Vertical"
        )
        logging.debug(info)
        assert False

    res1 = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}_1"
        ).splitlines()
    ]
    res2 = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}_2"
        ).splitlines()
    ]
    res3 = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}_3"
        ).splitlines()
    ]
    assert {tuple(v) for v in res1 + res2 + res3} == set(
        [tuple(i) for i in total_values]
    )

    # Checking that all files were processed only once
    time.sleep(10)
    assert (
        get_count(f"{dst_table_name}_1")
        + get_count(f"{dst_table_name}_2")
        + get_count(f"{dst_table_name}_3")
    ) == files_to_generate


@pytest.mark.parametrize("mode", AVAILABLE_MODES)
def test_multiple_tables_streaming_sync_distributed(started_cluster, mode):
    node = started_cluster.instances["instance"]
    node_2 = started_cluster.instances["instance2"]
    # A unique table name is necessary for repeatable tests
    table_name = (
        f"multiple_tables_streaming_sync_distributed_{mode}_{generate_random_string()}"
    )
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 1000
    row_num = 10
    total_rows = row_num * files_to_generate

    for instance in [node, node_2]:
        create_table(
            started_cluster,
            instance,
            table_name,
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "s3queue_buckets": 2,
                "polling_max_timeout_ms": 2000,
                "polling_backoff_ms": 1000,
                **({"s3queue_processing_threads_num": 1} if mode == "ordered" else {}),
            },
        )

    for instance in [node, node_2]:
        create_mv(instance, table_name, dst_table_name)

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, row_num=row_num
    )

    def get_count(node, table_name):
        return int(run_query(node, f"SELECT count() FROM {table_name}"))

    for _ in range(150):
        if (
            get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
        ) == total_rows:
            break
        time.sleep(1)

    if (
        get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
    ) != total_rows:
        info = node.query(
            f"SELECT * FROM system.s3queue WHERE zookeeper_path like '%{table_name}' ORDER BY file_name FORMAT Vertical"
        )
        logging.debug(info)
        assert False

    get_query = f"SELECT column1, column2, column3 FROM {dst_table_name}"
    res1 = [list(map(int, l.split())) for l in run_query(node, get_query).splitlines()]
    res2 = [
        list(map(int, l.split())) for l in run_query(node_2, get_query).splitlines()
    ]

    logging.debug(
        f"res1 size: {len(res1)}, res2 size: {len(res2)}, total_rows: {total_rows}"
    )

    assert len(res1) + len(res2) == total_rows

    # Checking that all engines have made progress
    assert len(res1) > 0
    assert len(res2) > 0

    assert {tuple(v) for v in res1 + res2} == set([tuple(i) for i in total_values])

    # Checking that all files were processed only once
    time.sleep(10)
    assert (
        get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
    ) == total_rows


def test_max_set_age(started_cluster):
    node = started_cluster.instances["instance"]
    table_name = "max_set_age"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    max_age = 20
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "unordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "tracked_file_ttl_sec": max_age,
            "cleanup_interval_min_ms": max_age / 3,
            "cleanup_interval_max_ms": max_age / 3,
            "polling_max_timeout_ms": 5000,
            "polling_backoff_ms": 1000,
            "processing_threads_num": 1,
        },
    )
    create_mv(node, table_name, dst_table_name)

    _ = generate_random_files(started_cluster, files_path, files_to_generate, row_num=1)

    expected_rows = files_to_generate

    node.wait_for_log_line("Checking node limits")
    node.wait_for_log_line("Node limits check finished")

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    def wait_for_condition(check_function, max_wait_time=1.5 * max_age):
        before = time.time()
        while time.time() - before < max_wait_time:
            if check_function():
                return
            time.sleep(0.25)
        assert False

    wait_for_condition(lambda: get_count() == expected_rows)
    assert files_to_generate == int(
        node.query(f"SELECT uniq(_path) from {dst_table_name}")
    )

    expected_rows *= 2
    wait_for_condition(lambda: get_count() == expected_rows)
    assert files_to_generate == int(
        node.query(f"SELECT uniq(_path) from {dst_table_name}")
    )

    paths_count = [
        int(x)
        for x in node.query(
            f"SELECT count() from {dst_table_name} GROUP BY _path"
        ).splitlines()
    ]
    assert files_to_generate == len(paths_count)
    for path_count in paths_count:
        assert 2 == path_count

    def get_object_storage_failures():
        return int(
            node.query(
                "SELECT value FROM system.events WHERE name = 'ObjectStorageQueueFailedFiles' SETTINGS system_events_show_zero_values=1"
            )
        )

    failed_count = get_object_storage_failures()

    values = [
        ["failed", 1, 1],
    ]
    values_csv = (
        "\n".join((",".join(map(str, row)) for row in values)) + "\n"
    ).encode()

    # use a different filename for each test to allow running a bunch of them sequentially with --count
    file_with_error = f"max_set_age_fail_{uuid.uuid4().hex[:8]}.csv"
    put_s3_file_content(started_cluster, f"{files_path}/{file_with_error}", values_csv)

    wait_for_condition(lambda: failed_count + 1 == get_object_storage_failures())

    node.query("SYSTEM FLUSH LOGS")
    assert "Cannot parse input" in node.query(
        f"SELECT exception FROM system.s3queue WHERE file_name ilike '%{file_with_error}'"
    )
    assert "Cannot parse input" in node.query(
        f"SELECT exception FROM system.s3queue_log WHERE file_name ilike '%{file_with_error}' ORDER BY processing_end_time DESC LIMIT 1"
    )

    assert 1 == int(
        node.query(
            f"SELECT count() FROM system.s3queue_log WHERE file_name ilike '%{file_with_error}' AND notEmpty(exception)"
        )
    )

    wait_for_condition(lambda: failed_count + 2 == get_object_storage_failures())

    node.query("SYSTEM FLUSH LOGS")
    assert "Cannot parse input" in node.query(
        f"SELECT exception FROM system.s3queue WHERE file_name ilike '%{file_with_error}' ORDER BY processing_end_time DESC LIMIT 1"
    )
    assert 1 < int(
        node.query(
            f"SELECT count() FROM system.s3queue_log WHERE file_name ilike '%{file_with_error}' AND notEmpty(exception)"
        )
    )

    node.restart_clickhouse()

    expected_rows *= 2
    wait_for_condition(lambda: get_count() == expected_rows)
    assert files_to_generate == int(
        node.query(f"SELECT uniq(_path) from {dst_table_name}")
    )


def test_max_set_size(started_cluster):
    node = started_cluster.instances["instance"]
    table_name = f"max_set_size"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "unordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_tracked_files_limit": 9,
            "s3queue_cleanup_interval_min_ms": 0,
            "s3queue_cleanup_interval_max_ms": 0,
            "s3queue_processing_threads_num": 1,
        },
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    get_query = f"SELECT * FROM {table_name} ORDER BY column1, column2, column3"
    res1 = [list(map(int, l.split())) for l in run_query(node, get_query).splitlines()]
    assert res1 == sorted(total_values, key=lambda x: (x[0], x[1], x[2]))
    print(total_values)

    time.sleep(10)

    zk = started_cluster.get_kazoo_client("zoo1")
    processed_nodes = zk.get_children(f"{keeper_path}/processed/")
    assert len(processed_nodes) == 9

    res1 = [list(map(int, l.split())) for l in run_query(node, get_query).splitlines()]
    assert res1 == [total_values[0]]

    time.sleep(10)
    res1 = [list(map(int, l.split())) for l in run_query(node, get_query).splitlines()]
    assert res1 == [total_values[1]]


def test_drop_table(started_cluster):
    node = started_cluster.instances["instance"]
    table_name = f"test_drop"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 300

    create_table(
        started_cluster,
        node,
        table_name,
        "unordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": 5,
        },
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=100000
    )
    create_mv(node, table_name, dst_table_name)
    node.wait_for_log_line(f"rows from file: test_drop_data")
    node.query(f"DROP TABLE {table_name} SYNC")
    assert node.contains_in_log(
        f"StorageS3Queue (default.{table_name}): Table is being dropped"
    ) or node.contains_in_log(
        f"StorageS3Queue (default.{table_name}): Shutdown was called"
    )


def test_s3_client_reused(started_cluster):
    node = started_cluster.instances["instance"]
    table_name = f"test_s3_client_reused"
    dst_table_name = f"{table_name}_dst"
    files_path = f"{table_name}_data"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    row_num = 10

    def get_created_s3_clients_count():
        value = node.query(
            f"SELECT value FROM system.events WHERE event='S3Clients'"
        ).strip()
        return int(value) if value != "" else 0

    def wait_all_processed(files_num):
        expected_count = files_num * row_num
        for _ in range(100):
            count = int(node.query(f"SELECT count() FROM {dst_table_name}"))
            print(f"{count}/{expected_count}")
            if count == expected_count:
                break
            time.sleep(1)
        assert (
            int(node.query(f"SELECT count() FROM {dst_table_name}")) == expected_count
        )

    prepare_public_s3_bucket(started_cluster)

    s3_clients_before = get_created_s3_clients_count()

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        additional_settings={
            "after_processing": "delete",
            "s3queue_processing_threads_num": 1,
            "keeper_path": keeper_path,
        },
        auth=NO_AUTH,
        bucket=started_cluster.minio_public_bucket,
    )

    s3_clients_after = get_created_s3_clients_count()
    assert s3_clients_before + 1 == s3_clients_after

    create_mv(node, table_name, dst_table_name)

    for i in range(0, 10):
        s3_clients_before = get_created_s3_clients_count()

        generate_random_files(
            started_cluster,
            files_path,
            count=1,
            start_ind=i,
            row_num=row_num,
            bucket=started_cluster.minio_public_bucket,
        )

        wait_all_processed(i + 1)

        s3_clients_after = get_created_s3_clients_count()

        assert s3_clients_before == s3_clients_after


def get_processed_files(node, table_name):
    return (
        node.query(
            f"""
select splitByChar('/', file_name)[-1] as file
from system.s3queue where zookeeper_path ilike '%{table_name}%' and status = 'Processed' order by file
        """
        )
        .strip()
        .split("\n")
    )


def get_unprocessed_files(node, table_name):
    return node.query(
        f"""
        select concat('test_',  toString(number), '.csv') as file from numbers(300)
        where file not
        in (select splitByChar('/', file_name)[-1] from system.s3queue where zookeeper_path ilike '%{table_name}%' and status = 'Processed')
        """
    )


@pytest.mark.parametrize("mode", ["unordered", "ordered"])
def test_processing_threads(started_cluster, mode):
    node = started_cluster.instances["instance"]
    table_name = f"processing_threads_{mode}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 300
    processing_threads = 32

    create_table(
        started_cluster,
        node,
        table_name,
        mode,
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": processing_threads,
        },
    )
    create_mv(node, table_name, dst_table_name)

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, row_num=1
    )

    def get_count(table_name):
        return int(run_query(node, f"SELECT count() FROM {table_name}"))

    for _ in range(50):
        if (get_count(f"{dst_table_name}")) == files_to_generate:
            break
        time.sleep(1)

    if get_count(dst_table_name) != files_to_generate:
        processed_files = get_processed_files(node, table_name)
        unprocessed_files = get_unprocessed_files(node, table_name)
        logging.debug(
            f"Processed files: {len(processed_files)}/{files_to_generate}, unprocessed files: {unprocessed_files}, count: {get_count(dst_table_name)}"
        )
        assert False

    res = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}"
        ).splitlines()
    ]
    assert {tuple(v) for v in res} == set([tuple(i) for i in total_values])

    if mode == "ordered":
        zk = started_cluster.get_kazoo_client("zoo1")
        nodes = zk.get_children(f"{keeper_path}")
        print(f"Metadata nodes: {nodes}")
        processed_nodes = zk.get_children(f"{keeper_path}/buckets/")
        assert len(processed_nodes) == processing_threads


@pytest.mark.parametrize(
    "mode, processing_threads",
    [
        pytest.param("unordered", 1),
        pytest.param("unordered", 8),
        pytest.param("ordered", 1),
        pytest.param("ordered", 8),
    ],
)
def test_shards(started_cluster, mode, processing_threads):
    node = started_cluster.instances["instance"]
    table_name = f"test_shards_{mode}_{processing_threads}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 300
    shards_num = 3

    for i in range(shards_num):
        table = f"{table_name}_{i + 1}"
        dst_table = f"{dst_table_name}_{i + 1}"
        create_table(
            started_cluster,
            node,
            table,
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "s3queue_processing_threads_num": processing_threads,
                "s3queue_buckets": shards_num,
            },
        )
        create_mv(node, table, dst_table)

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, row_num=1
    )

    def get_count(table_name):
        return int(run_query(node, f"SELECT count() FROM {table_name}"))

    for _ in range(30):
        count = (
            get_count(f"{dst_table_name}_1")
            + get_count(f"{dst_table_name}_2")
            + get_count(f"{dst_table_name}_3")
        )
        if count == files_to_generate:
            break
        print(f"Current {count}/{files_to_generate}")
        time.sleep(1)

    if (
        get_count(f"{dst_table_name}_1")
        + get_count(f"{dst_table_name}_2")
        + get_count(f"{dst_table_name}_3")
    ) != files_to_generate:
        processed_files = (
            node.query(
                f"""
select splitByChar('/', file_name)[-1] as file from system.s3queue
where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0 order by file
                """
            )
            .strip()
            .split("\n")
        )
        logging.debug(
            f"Processed files: {len(processed_files)}/{files_to_generate}: {processed_files}"
        )

        count = (
            get_count(f"{dst_table_name}_1")
            + get_count(f"{dst_table_name}_2")
            + get_count(f"{dst_table_name}_3")
        )
        logging.debug(f"Processed rows: {count}/{files_to_generate}")

        info = node.query(
            f"""
            select concat('test_',  toString(number), '.csv') as file from numbers(300)
            where file not in (select splitByChar('/', file_name)[-1] from system.s3queue
            where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0)
            """
        )
        logging.debug(f"Unprocessed files: {info}")

        assert False

    res1 = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}_1"
        ).splitlines()
    ]
    res2 = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}_2"
        ).splitlines()
    ]
    res3 = [
        list(map(int, l.split()))
        for l in node.query(
            f"SELECT column1, column2, column3 FROM {dst_table_name}_3"
        ).splitlines()
    ]
    assert {tuple(v) for v in res1 + res2 + res3} == set(
        [tuple(i) for i in total_values]
    )

    # Checking that all files were processed only once
    time.sleep(10)
    assert (
        get_count(f"{dst_table_name}_1")
        + get_count(f"{dst_table_name}_2")
        + get_count(f"{dst_table_name}_3")
    ) == files_to_generate

    if mode == "ordered":
        zk = started_cluster.get_kazoo_client("zoo1")
        processed_nodes = zk.get_children(f"{keeper_path}/buckets/")
        assert len(processed_nodes) == shards_num


@pytest.mark.parametrize(
    "mode, processing_threads",
    [
        pytest.param("unordered", 1),
        pytest.param("unordered", 8),
        pytest.param("ordered", 1),
        pytest.param("ordered", 2),
    ],
)
def test_shards_distributed(started_cluster, mode, processing_threads):
    node = started_cluster.instances["instance"]
    node_2 = started_cluster.instances["instance2"]
    table_name = f"test_shards_distributed_{mode}_{processing_threads}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 600
    row_num = 1000
    total_rows = row_num * files_to_generate
    shards_num = 2

    i = 0
    for instance in [node, node_2]:
        create_table(
            started_cluster,
            instance,
            table_name,
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "s3queue_processing_threads_num": processing_threads,
                "s3queue_buckets": shards_num,
                "polling_max_timeout_ms": 1000,
                "polling_backoff_ms": 0,
            },
        )
        i += 1

    for instance in [node, node_2]:
        create_mv(instance, table_name, dst_table_name)

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, row_num=row_num
    )

    def get_count(node, table_name):
        return int(run_query(node, f"SELECT count() FROM {table_name}"))

    def print_debug_info():
        processed_files = (
            node.query(
                f"""
select splitByChar('/', file_name)[-1] as file from system.s3queue where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0 order by file
            """
            )
            .strip()
            .split("\n")
        )
        logging.debug(
            f"Processed files by node 1: {len(processed_files)}/{files_to_generate}"
        )
        processed_files = (
            node_2.query(
                f"""
select splitByChar('/', file_name)[-1] as file from system.s3queue where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0 order by file
            """
            )
            .strip()
            .split("\n")
        )
        logging.debug(
            f"Processed files by node 2: {len(processed_files)}/{files_to_generate}"
        )

        count = get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
        logging.debug(f"Processed rows: {count}/{total_rows}")

        info = node.query(
            f"""
            select concat('test_',  toString(number), '.csv') as file from numbers(300)
            where file not in (select splitByChar('/', file_name)[-1] from clusterAllReplicas(default, system.s3queue)
            where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0)
            """
        )
        logging.debug(f"Unprocessed files: {info}")

        files1 = (
            node.query(
                f"""
            select splitByChar('/', file_name)[-1] from system.s3queue
            where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0
            """
            )
            .strip()
            .split("\n")
        )
        files2 = (
            node_2.query(
                f"""
            select splitByChar('/', file_name)[-1] from system.s3queue
            where zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0
            """
            )
            .strip()
            .split("\n")
        )

        def intersection(list_a, list_b):
            return [e for e in list_a if e in list_b]

        logging.debug(f"Intersecting files: {intersection(files1, files2)}")

    for _ in range(30):
        if (
            get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
        ) == total_rows:
            break
        time.sleep(1)

    if (
        get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
    ) != total_rows:
        print_debug_info()

        assert False

    get_query = f"SELECT column1, column2, column3 FROM {dst_table_name}"
    res1 = [list(map(int, l.split())) for l in run_query(node, get_query).splitlines()]
    res2 = [
        list(map(int, l.split())) for l in run_query(node_2, get_query).splitlines()
    ]

    if len(res1) + len(res2) != total_rows or len(res1) <= 0 or len(res2) <= 0 or True:
        logging.debug(
            f"res1 size: {len(res1)}, res2 size: {len(res2)}, total_rows: {total_rows}"
        )
        print_debug_info()

    assert len(res1) + len(res2) == total_rows

    # Checking that all engines have made progress
    assert len(res1) > 0
    assert len(res2) > 0

    assert {tuple(v) for v in res1 + res2} == set([tuple(i) for i in total_values])

    # Checking that all files were processed only once
    time.sleep(10)
    assert (
        get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
    ) == total_rows

    if mode == "ordered":
        zk = started_cluster.get_kazoo_client("zoo1")
        processed_nodes = zk.get_children(f"{keeper_path}/buckets/")
        assert len(processed_nodes) == shards_num

    node.restart_clickhouse()
    time.sleep(10)
    assert (
        get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
    ) == total_rows


def test_settings_check(started_cluster):
    node = started_cluster.instances["instance"]
    node_2 = started_cluster.instances["instance2"]
    table_name = f"test_settings_check"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    mode = "ordered"

    create_table(
        started_cluster,
        node,
        table_name,
        mode,
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": 5,
            "s3queue_buckets": 2,
        },
    )

    assert (
        "Existing table metadata in ZooKeeper differs in buckets setting. Stored in ZooKeeper: 2, local: 3"
        in create_table(
            started_cluster,
            node_2,
            table_name,
            mode,
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "s3queue_processing_threads_num": 5,
                "s3queue_buckets": 3,
            },
            expect_error=True,
        )
    )

    node.query(f"DROP TABLE {table_name} SYNC")


@pytest.mark.parametrize("processing_threads", [1, 5])
def test_processed_file_setting(started_cluster, processing_threads):
    node = started_cluster.instances["instance"]
    table_name = f"test_processed_file_setting_{processing_threads}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = (
        f"/clickhouse/test_{table_name}_{processing_threads}_{generate_random_string()}"
    )
    files_path = f"{table_name}_data"
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": processing_threads,
            "s3queue_last_processed_path": f"{files_path}/test_5.csv",
        },
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node, table_name, dst_table_name)

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    expected_rows = 4
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)

    assert expected_rows == get_count()

    node.restart_clickhouse()
    time.sleep(10)

    expected_rows = 4
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)

    assert expected_rows == get_count()


@pytest.mark.parametrize("processing_threads", [1, 5])
def test_processed_file_setting_distributed(started_cluster, processing_threads):
    node = started_cluster.instances["instance"]
    node_2 = started_cluster.instances["instance2"]
    table_name = f"test_processed_file_setting_distributed_{processing_threads}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = (
        f"/clickhouse/test_{table_name}_{processing_threads}_{generate_random_string()}"
    )
    files_path = f"{table_name}_data"
    files_to_generate = 10

    for instance in [node, node_2]:
        create_table(
            started_cluster,
            instance,
            table_name,
            "ordered",
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "s3queue_processing_threads_num": processing_threads,
                "s3queue_last_processed_path": f"{files_path}/test_5.csv",
                "s3queue_buckets": 2,
                "polling_max_timeout_ms": 2000,
                "polling_backoff_ms": 1000,
            },
        )

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    for instance in [node, node_2]:
        create_mv(instance, table_name, dst_table_name)

    def get_count():
        query = f"SELECT count() FROM {dst_table_name}"
        return int(node.query(query)) + int(node_2.query(query))

    expected_rows = 4
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)
    assert expected_rows == get_count()

    for instance in [node, node_2]:
        instance.restart_clickhouse()

    time.sleep(10)
    expected_rows = 4
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)
    assert expected_rows == get_count()


def test_upgrade(started_cluster):
    node = started_cluster.instances["old_instance"]

    table_name = f"test_upgrade"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}_{generate_random_string()}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        version="23.12",
        additional_settings={
            "keeper_path": keeper_path,
            "after_processing": "keep",
        },
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node, table_name, dst_table_name)

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    expected_rows = 10
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)

    assert expected_rows == get_count()

    node.restart_with_latest_version()

    assert expected_rows == get_count()


def test_exception_during_insert(started_cluster):
    node = started_cluster.instances["instance_too_many_parts"]

    # A unique table name is necessary for repeatable tests
    table_name = f"test_exception_during_insert_{generate_random_string()}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"

    create_table(
        started_cluster,
        node,
        table_name,
        "unordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "polling_min_timeout_ms": 100,
            "polling_max_timeout_ms": 100,
            "polling_backoff_ms": 0,
        },
    )
    node.rotate_logs()

    node.query("system stop merges")
    create_mv(node, table_name, dst_table_name)

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    def wait_for_rows(expected_rows):
        for _ in range(20):
            if expected_rows == get_count():
                break
            time.sleep(1)
        assert expected_rows == get_count()

    expected_rows = [0]

    def generate(check_inserted):
        files_to_generate = 1
        row_num = 1
        time.sleep(10)
        total_values = generate_random_files(
            started_cluster,
            files_path,
            files_to_generate,
            start_ind=0,
            row_num=row_num,
            use_random_names=1,
        )
        expected_rows[0] += files_to_generate * row_num
        if check_inserted:
            wait_for_rows(expected_rows[0])

    generate(True)
    generate(True)
    generate(True)
    generate(False)

    node.wait_for_log_line(
        "Merges are processing significantly slower than inserts: while pushing to view default.test_exception_during_insert_"
    )

    time.sleep(2)
    exception = node.query(
        f"SELECT exception FROM system.s3queue WHERE zookeeper_path ilike '%{table_name}%' and notEmpty(exception)"
    )
    assert "Too many parts" in exception

    node.query("system start merges")
    node.query(f"optimize table {dst_table_name} final")

    wait_for_rows(expected_rows[0])


@pytest.mark.parametrize("processing_threads", [1, 8])
def test_commit_on_limit(started_cluster, processing_threads):
    node = started_cluster.instances["instance"]

    # A unique table name is necessary for repeatable tests
    table_name = f"test_commit_on_limit_{generate_random_string()}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    failed_files_event_before = int(
        node.query(
            "SELECT value FROM system.events WHERE name = 'ObjectStorageQueueFailedFiles' SETTINGS system_events_show_zero_values=1"
        )
    )
    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": processing_threads,
            "s3queue_loading_retries": 0,
            "s3queue_max_processed_files_before_commit": 10,
        },
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    incorrect_values = [
        ["failed", 1, 1],
    ]
    incorrect_values_csv = (
        "\n".join((",".join(map(str, row)) for row in incorrect_values)) + "\n"
    ).encode()

    correct_values = [
        [1, 1, 1],
    ]
    correct_values_csv = (
        "\n".join((",".join(map(str, row)) for row in correct_values)) + "\n"
    ).encode()

    put_s3_file_content(
        started_cluster, f"{files_path}/test_99.csv", correct_values_csv
    )
    put_s3_file_content(
        started_cluster, f"{files_path}/test_999.csv", correct_values_csv
    )
    put_s3_file_content(
        started_cluster, f"{files_path}/test_9999.csv", incorrect_values_csv
    )
    put_s3_file_content(
        started_cluster, f"{files_path}/test_99999.csv", correct_values_csv
    )
    put_s3_file_content(
        started_cluster, f"{files_path}/test_999999.csv", correct_values_csv
    )

    create_mv(node, table_name, dst_table_name)

    def get_processed_files():
        return (
            node.query(
                f"SELECT file_name FROM system.s3queue WHERE zookeeper_path ilike '%{table_name}%' and status = 'Processed' and rows_processed > 0 "
            )
            .strip()
            .split("\n")
        )

    def get_failed_files():
        return (
            node.query(
                f"SELECT file_name FROM system.s3queue WHERE zookeeper_path ilike '%{table_name}%' and status = 'Failed'"
            )
            .strip()
            .split("\n")
        )

    for _ in range(30):
        if "test_999999.csv" in get_processed_files():
            break
        time.sleep(1)

    assert "test_999999.csv" in get_processed_files()

    assert 1 == int(
        node.count_in_log(f"Setting file {files_path}/test_9999.csv as failed")
    )
    assert 1 == int(
        node.count_in_log(
            f"File {files_path}/test_9999.csv failed to process and will not be retried"
        )
    )

    assert failed_files_event_before + 1 == int(
        node.query(
            "SELECT value FROM system.events WHERE name = 'ObjectStorageQueueFailedFiles' SETTINGS system_events_show_zero_values=1"
        )
    )

    expected_processed = ["test_" + str(i) + ".csv" for i in range(files_to_generate)]
    processed = get_processed_files()
    for value in expected_processed:
        assert value in processed

    expected_failed = ["test_9999.csv"]
    failed = get_failed_files()
    for value in expected_failed:
        assert value not in processed
        assert value in failed


def test_upgrade_2(started_cluster):
    node = started_cluster.instances["instance_24.5"]
    assert "24.5" in node.query("select version()").strip()

    table_name = f"test_upgrade_2_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        version="24.5",
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_current_shard_num": 0,
            "s3queue_processing_threads_num": 2,
        },
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node, table_name, dst_table_name)

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    expected_rows = 10
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)

    assert expected_rows == get_count()

    node.restart_with_latest_version()
    assert table_name in node.query("SHOW TABLES")


def test_replicated(started_cluster):
    node1 = started_cluster.instances["node1"]
    node2 = started_cluster.instances["node2"]

    table_name = f"test_replicated_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 1000

    node1.query("DROP DATABASE IF EXISTS r")
    node2.query("DROP DATABASE IF EXISTS r")

    node1.query(
        "CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/replicateddb', 'shard1', 'node1')"
    )
    node2.query(
        "CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/replicateddb', 'shard1', 'node2')"
    )

    create_table(
        started_cluster,
        node1,
        table_name,
        "ordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
        },
        database_name="r",
    )

    assert '"processing_threads_num":16' in node1.query(
        f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
    )

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node1, f"r.{table_name}", dst_table_name)
    create_mv(node2, f"r.{table_name}", dst_table_name)

    def get_count():
        return int(
            node1.query(
                f"SELECT count() FROM clusterAllReplicas(cluster, default.{dst_table_name})"
            )
        )

    expected_rows = files_to_generate
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)
    assert expected_rows == get_count()


def test_bad_settings(started_cluster):
    node = started_cluster.instances["node_cloud_mode"]

    table_name = f"test_bad_settings_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    try:
        create_table(
            started_cluster,
            node,
            table_name,
            "ordered",
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "processing_threads_num": 1,
                "buckets": 0,
            },
        )
        assert False
    except Exception as e:
        assert "Ordered mode in cloud without either" in str(e)


def test_processing_threads(started_cluster):
    node = started_cluster.instances["node1"]

    table_name = f"test_processing_threads_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    # A unique path is necessary for repeatable tests
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
        },
    )

    assert '"processing_threads_num":16' in node.query(
        f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
    )

    assert 16 == int(
        node.query(
            f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'processing_threads_num'"
        )
    )

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node, table_name, dst_table_name)

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    expected_rows = 10
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)

    assert expected_rows == get_count()

    assert node.contains_in_log(
        f"StorageS3Queue (default.{table_name}): Using 16 processing threads"
    )


def test_alter_settings(started_cluster):
    node1 = started_cluster.instances["node1"]
    node2 = started_cluster.instances["node2"]

    table_name = f"test_alter_settings_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 1000

    node1.query("DROP DATABASE IF EXISTS r")
    node2.query("DROP DATABASE IF EXISTS r")

    node1.query(
        f"CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/{table_name}', 'shard1', 'node1')"
    )
    node2.query(
        f"CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/{table_name}', 'shard1', 'node2')"
    )

    create_table(
        started_cluster,
        node1,
        table_name,
        "unordered",
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_processing_threads_num": 10,
            "s3queue_loading_retries": 20,
            "s3queue_tracked_files_limit": 1000,
        },
        database_name="r",
    )

    assert '"processing_threads_num":10' in node1.query(
        f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
    )

    assert '"loading_retries":20' in node1.query(
        f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
    )

    assert '"after_processing":"keep"' in node1.query(
        f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
    )

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node1, f"r.{table_name}", dst_table_name)
    create_mv(node2, f"r.{table_name}", dst_table_name)

    def get_count():
        return int(
            node1.query(
                f"SELECT count() FROM clusterAllReplicas(cluster, default.{dst_table_name})"
            )
        )

    expected_rows = files_to_generate
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)
    assert expected_rows == get_count()

    assert (
        "true"
        == node1.query(
            f"SELECT value FROM system.s3_queue_settings WHERE name = 'enable_hash_ring_filtering' and table = '{table_name}'"
        ).strip()
    )

    node1.query(
        f"""
        ALTER TABLE r.{table_name}
        MODIFY SETTING processing_threads_num=5,
        loading_retries=44,
        after_processing='delete',
        tracked_files_limit=50,
        tracked_file_ttl_sec=10000,
        polling_min_timeout_ms=222,
        s3queue_polling_max_timeout_ms=333,
        polling_backoff_ms=111,
        max_processed_files_before_commit=444,
        s3queue_max_processed_rows_before_commit=555,
        max_processed_bytes_before_commit=666,
        max_processing_time_sec_before_commit=777,
        enable_hash_ring_filtering=false,
        list_objects_batch_size=1234
    """
    )

    int_settings = {
        "processing_threads_num": 5,
        "loading_retries": 44,
        "tracked_files_ttl_sec": 10000,
        "tracked_files_limit": 50,
        "polling_min_timeout_ms": 222,
        "polling_max_timeout_ms": 333,
        "polling_backoff_ms": 111,
        "max_processed_files_before_commit": 444,
        "max_processed_rows_before_commit": 555,
        "max_processed_bytes_before_commit": 666,
        "max_processing_time_sec_before_commit": 777,
        "enable_hash_ring_filtering": "false",
        "list_objects_batch_size": 1234,
    }
    string_settings = {"after_processing": "delete"}

    def with_keeper(setting):
        return setting in {
            "after_processing",
            "loading_retries",
            "processing_threads_num",
            "tracked_files_limit",
            "tracked_files_ttl_sec",
        }

    def check_int_settings(node, settings):
        for setting, value in settings.items():
            if with_keeper(setting):
                assert f'"{setting}":{value}' in node.query(
                    f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
                )
            if setting == "tracked_files_ttl_sec":
                setting = "tracked_file_ttl_sec"
            assert (
                str(value)
                == node.query(
                    f"SELECT value FROM system.s3_queue_settings WHERE name = '{setting}' and table = '{table_name}'"
                ).strip()
            )

    def check_string_settings(node, settings):
        for setting, value in settings.items():
            if with_keeper(setting):
                assert f'"{setting}":"{value}"' in node.query(
                    f"SELECT * FROM system.zookeeper WHERE path = '{keeper_path}'"
                )
            assert (
                str(value)
                == node.query(
                    f"SELECT value FROM system.s3_queue_settings WHERE name = '{setting}' and table = '{table_name}'"
                ).strip()
            )

    for node in [node1, node2]:
        check_int_settings(node, int_settings)
        check_string_settings(node, string_settings)

        node.restart_clickhouse()

        check_int_settings(node, int_settings)
        check_string_settings(node, string_settings)

    node1.query(
        f"""
        ALTER TABLE r.{table_name} RESET SETTING after_processing, tracked_file_ttl_sec, loading_retries, s3queue_tracked_files_limit
    """
    )

    int_settings = {
        "processing_threads_num": 5,
        "loading_retries": 10,
        "tracked_files_ttl_sec": 0,
        "tracked_files_limit": 1000,
    }
    string_settings = {"after_processing": "keep"}

    for node in [node1, node2]:
        check_int_settings(node, int_settings)
        check_string_settings(node, string_settings)

        node.restart_clickhouse()

        check_int_settings(node, int_settings)
        check_string_settings(node, string_settings)


def test_list_and_delete_race(started_cluster):
    node = started_cluster.instances["instance"]
    if node.is_built_with_sanitizer():
        # Issue does not reproduce under sanitizer
        return
    node_2 = started_cluster.instances["instance2"]
    table_name = f"list_and_delete_race_{generate_random_string()}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 1000
    row_num = 1

    for instance in [node, node_2]:
        create_table(
            started_cluster,
            instance,
            table_name,
            "unordered",
            files_path,
            additional_settings={
                "keeper_path": keeper_path,
                "tracked_files_limit": 1,
                "polling_max_timeout_ms": 0,
                "processing_threads_num": 1,
                "polling_min_timeout_ms": 200,
                "cleanup_interval_min_ms": 0,
                "cleanup_interval_max_ms": 0,
                "polling_backoff_ms": 100,
                "after_processing": "delete",
            },
        )

    threads = 10
    total_rows = row_num * files_to_generate * (threads + 1)

    busy_pool = Pool(threads)

    def generate(_):
        generate_random_files(
            started_cluster,
            files_path,
            files_to_generate,
            row_num=row_num,
            use_random_names=True,
        )

    generate(0)

    p = busy_pool.map_async(generate, range(threads))

    create_mv(node, table_name, dst_table_name)
    time.sleep(2)
    create_mv(node_2, table_name, dst_table_name)

    p.wait()

    def get_count(node, table_name):
        return int(run_query(node, f"SELECT count() FROM {table_name}"))

    for _ in range(150):
        if (
            get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
        ) == total_rows:
            break
        time.sleep(1)

    assert (
        get_count(node, dst_table_name) + get_count(node_2, dst_table_name)
        == total_rows
    )

    get_query = f"SELECT column1, column2, column3 FROM {dst_table_name}"
    res1 = [list(map(int, l.split())) for l in run_query(node, get_query).splitlines()]
    res2 = [
        list(map(int, l.split())) for l in run_query(node_2, get_query).splitlines()
    ]

    logging.debug(
        f"res1 size: {len(res1)}, res2 size: {len(res2)}, total_rows: {total_rows}"
    )

    assert len(res1) + len(res2) == total_rows
    # (kssenii) I will fix this assert a tiny bit later
    # assert (
    #    node.contains_in_log("because of the race with list & delete")
    #    or node_2.contains_in_log("because of the race with list & delete")
    #    or node.contains_in_log(
    #        f"StorageS3Queue (default.{table_name}): Skipping file"  # Unfortunately this optimization makes the race less easy to catch.
    #    )
    #    or node_2.contains_in_log(
    #        f"StorageS3Queue (default.{table_name}): Skipping file"
    #    )
    # )


def test_registry(started_cluster):
    node1 = started_cluster.instances["node1"]
    node2 = started_cluster.instances["node2"]

    table_name = f"test_registry_{uuid.uuid4().hex[:8]}"
    db_name = f"db_{table_name}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 1000

    node1.query(f"DROP DATABASE IF EXISTS {db_name}")
    node2.query(f"DROP DATABASE IF EXISTS {db_name}")

    node1.query(
        f"CREATE DATABASE {db_name} ENGINE=Replicated('/clickhouse/databases/replicateddb2', 'shard1', 'node1')"
    )
    node2.query(
        f"CREATE DATABASE {db_name} ENGINE=Replicated('/clickhouse/databases/replicateddb2', 'shard1', 'node2')"
    )

    create_table(
        started_cluster,
        node1,
        table_name,
        "ordered",
        files_path,
        additional_settings={"keeper_path": keeper_path, "buckets": 3},
        database_name=db_name,
    )

    zk = started_cluster.get_kazoo_client("zoo1")
    registry, stat = zk.get(f"{keeper_path}/registry/")

    uuid1 = node1.query(
        f"SELECT uuid FROM system.tables WHERE database = '{db_name}' and table = '{table_name}'"
    ).strip()
    assert uuid1 in str(registry)

    expected = [f"0\\nnode1\\n{uuid1}\\n", f"0\\nnode2\\n{uuid1}\\n"]

    for elem in expected:
        assert elem in str(registry)

    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node1, f"{db_name}.{table_name}", dst_table_name)
    create_mv(node2, f"{db_name}.{table_name}", dst_table_name)

    def get_count():
        return int(
            node1.query(
                f"SELECT count() FROM clusterAllReplicas(cluster, default.{dst_table_name})"
            )
        )

    expected_rows = files_to_generate
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)
    assert expected_rows == get_count()

    table_name_2 = f"test_registry_{uuid.uuid4().hex[:8]}_2"
    create_table(
        started_cluster,
        node1,
        table_name_2,
        "ordered",
        files_path,
        additional_settings={"keeper_path": keeper_path, "buckets": 3},
        database_name=db_name,
    )

    registry, stat = zk.get(f"{keeper_path}/registry/")

    uuid2 = node1.query(
        f"SELECT uuid FROM system.tables WHERE database = '{db_name}' and table = '{table_name_2}'"
    ).strip()

    assert uuid1 in str(registry)
    assert uuid2 in str(registry)

    expected = [
        f"0\\nnode1\\n{uuid1}\\n",
        f"0\\nnode2\\n{uuid1}\\n",
        f"0\\nnode1\\n{uuid2}\\n",
        f"0\\nnode2\\n{uuid2}\\n",
    ]

    for elem in expected:
        assert elem in str(registry)

    node1.restart_clickhouse()
    node2.restart_clickhouse()

    registry, stat = zk.get(f"{keeper_path}/registry/")

    assert uuid1 in str(registry)
    assert uuid2 in str(registry)

    node1.query(f"DROP TABLE {db_name}.{table_name_2} SYNC")

    assert zk.exists(keeper_path) is not None
    registry, stat = zk.get(f"{keeper_path}/registry/")

    assert uuid1 in str(registry)
    assert uuid2 not in str(registry)

    expected = [
        f"0\\nnode1\\n{uuid1}\\n",
        f"0\\nnode2\\n{uuid1}\\n",
    ]

    for elem in expected:
        assert elem in str(registry)

    node1.query(f"DROP TABLE {db_name}.{table_name} SYNC")

    assert zk.exists(keeper_path) is None


def test_upgrade_3(started_cluster):
    node = started_cluster.instances["instance2_24.5"]
    assert "24.5" in node.query("select version()").strip()

    table_name = f"test_upgrade_3_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 10

    create_table(
        started_cluster,
        node,
        table_name,
        "ordered",
        files_path,
        no_settings=True,
        version="24.5",
    )
    total_values = generate_random_files(
        started_cluster, files_path, files_to_generate, start_ind=0, row_num=1
    )

    create_mv(node, table_name, dst_table_name)

    def get_count():
        return int(node.query(f"SELECT count() FROM {dst_table_name}"))

    expected_rows = 10
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)

    assert expected_rows == get_count()

    node.restart_with_latest_version()

    assert table_name in node.query("SHOW TABLES")

    node.query(
        f"""
        ALTER TABLE {table_name} MODIFY SETTING polling_min_timeout_ms=111
    """
    )
    assert 111 == int(
        node.query(
            f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'polling_min_timeout_ms'"
        )
    )

    node.query(
        f"""
        ALTER TABLE {table_name} MODIFY SETTING polling_min_timeout_ms=222, polling_max_timeout_ms=333
    """
    )
    assert 222 == int(
        node.query(
            f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'polling_min_timeout_ms'"
        )
    )
    assert 333 == int(
        node.query(
            f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'polling_max_timeout_ms'"
        )
    )

    assert "polling_max_timeout_ms = 333" in node.query(
        f"SHOW CREATE TABLE {table_name}"
    )

    node.restart_clickhouse()

    assert "polling_max_timeout_ms = 333" in node.query(
        f"SHOW CREATE TABLE {table_name}"
    )

    assert 333 == int(
        node.query(
            f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'polling_max_timeout_ms'"
        )
    )
    node.query(f"DROP TABLE {table_name} SYNC")


@pytest.mark.parametrize("setting_prefix", ["", "s3queue_"])
def test_migration(started_cluster, setting_prefix):
    node1 = started_cluster.instances["instance3_24.5"]
    node2 = started_cluster.instances["instance4_24.5"]

    for node in [node1, node2]:
        if "24.5" not in node.query("select version()").strip():
            node.restart_with_original_version()

    table_name = f"test_replicated_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    mv_name = f"{table_name}_mv"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"

    for node in [node1, node2]:
        node.query("DROP DATABASE IF EXISTS r")

    node1.query(
        "CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/replicateddb3', 'shard1', 'node1')"
    )
    node2.query(
        "CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/replicateddb3', 'shard1', 'node2')"
    )

    create_table(
        started_cluster,
        node1,
        table_name,
        "ordered",
        files_path,
        version="24.5",
        additional_settings={
            "keeper_path": keeper_path,
            "s3queue_polling_min_timeout_ms": 100,
            "s3queue_polling_max_timeout_ms": 1000,
            "s3queue_polling_backoff_ms": 100,
        },
        database_name="r",
    )

    for node in [node1, node2]:
        create_mv(node, f"r.{table_name}", dst_table_name, mv_name=mv_name)

    start_ind = [0]
    expected_rows = [0]
    last_processed_path = [""]
    prefix_ind = [0]
    prefixes = ["a", "b", "c", "d", "e"]

    def add_files_and_check():
        rows = 1000
        use_prefix = prefixes[prefix_ind[0]]
        total_values = generate_random_files(
            started_cluster,
            files_path,
            rows,
            start_ind=start_ind[0],
            row_num=1,
            use_prefix=use_prefix,
        )
        expected_rows[0] += rows
        start_ind[0] += rows
        prefix_ind[0] += 1

        def get_count():
            return int(
                node1.query(
                    f"SELECT count() FROM clusterAllReplicas(cluster, default.{dst_table_name})"
                )
            )

        last_processed_path[0] = f"{use_prefix}_{expected_rows[0] - 1}.csv"
        for _ in range(20):
            if expected_rows[0] == get_count():
                break
            time.sleep(1)
        assert expected_rows[0] == get_count()

    add_files_and_check()

    zk = started_cluster.get_kazoo_client("zoo1")
    metadata = json.loads(zk.get(f"{keeper_path}/processed")[0])

    assert last_processed_path[0].startswith("a_")
    assert metadata["file_path"].endswith(last_processed_path[0])

    for node in [node1, node2]:
        node.restart_with_latest_version()
        assert 0 == int(
            node.query(
                f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'buckets'"
            )
        )

    buckets_num = 3
    assert (
        "Changing setting buckets is not allowed only with detached dependencies"
        in node1.query_and_get_error(
            f"ALTER TABLE r.{table_name} MODIFY SETTING {setting_prefix}buckets={buckets_num}"
        )
    )

    for node in [node1, node2]:
        node.query(f"DETACH TABLE {mv_name} SYNC")

    assert (
        "To allow migration set s3queue_migrate_old_metadata_to_buckets = 1"
        in node1.query_and_get_error(
            f"ALTER TABLE r.{table_name} MODIFY SETTING {setting_prefix}buckets={buckets_num}"
        )
    )

    node1.query(
        f"ALTER TABLE r.{table_name} MODIFY SETTING {setting_prefix}buckets={buckets_num} SETTINGS s3queue_migrate_old_metadata_to_buckets = 1"
    )

    for node in [node1, node2]:
        assert buckets_num == int(
            node.query(
                f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'buckets'"
            )
        )

    metadata = json.loads(zk.get(f"{keeper_path}/metadata/")[0])
    assert buckets_num == metadata["buckets"]

    try:
        zk.get(f"{keeper_path}/processed")
        assert False
    except NoNodeError:
        pass

    buckets = zk.get_children(f"{keeper_path}/buckets/")

    assert len(buckets) == buckets_num
    assert sorted(buckets) == [str(i) for i in range(buckets_num)]

    for i in range(buckets_num):
        metadata = json.loads(zk.get(f"{keeper_path}/buckets/{i}/processed")[0])
        assert metadata["file_path"].endswith(last_processed_path[0])

    for node in [node1, node2]:
        node.query(f"ATTACH TABLE {mv_name}")

    add_files_and_check()

    for node in [node1, node2]:
        node.restart_clickhouse()
        assert buckets_num == int(
            node.query(
                f"SELECT value FROM system.s3_queue_settings WHERE table = '{table_name}' and name = 'buckets'"
            )
        )

    add_files_and_check()

    try:
        zk.get(f"{keeper_path}/processed")
        assert False
    except NoNodeError:
        pass

    buckets = zk.get_children(f"{keeper_path}/buckets/")
    assert len(buckets) == buckets_num

    found = False
    for i in range(buckets_num):
        metadata = json.loads(zk.get(f"{keeper_path}/buckets/{i}/processed")[0])
        if metadata["file_path"].endswith(last_processed_path[0]):
            found = True
            break
    assert found

    metadata = json.loads(zk.get(f"{keeper_path}/metadata/")[0])
    assert buckets_num == metadata["buckets"]

    node.query(f"DROP TABLE r.{table_name} SYNC")


@pytest.mark.parametrize("mode", ["unordered", "ordered"])
def test_filtering_files(started_cluster, mode):
    node1 = started_cluster.instances["node1"]
    node2 = started_cluster.instances["node2"]

    table_name = f"test_replicated_{mode}_{uuid.uuid4().hex[:8]}"
    dst_table_name = f"{table_name}_dst"
    keeper_path = f"/clickhouse/test_{table_name}"
    files_path = f"{table_name}_data"
    files_to_generate = 100

    node1.query("DROP DATABASE IF EXISTS r")
    node2.query("DROP DATABASE IF EXISTS r")

    node1.query(
        "CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/replicateddb4', 'shard1', 'node1')"
    )
    node2.query(
        "CREATE DATABASE r ENGINE=Replicated('/clickhouse/databases/replicateddb4', 'shard1', 'node2')"
    )

    create_table(
        started_cluster,
        node1,
        table_name,
        mode,
        files_path,
        additional_settings={
            "keeper_path": keeper_path,
            "polling_min_timeout_ms": 100,
            "polling_max_timeout_ms": 100,
            "polling_backoff_ms": 0,
        },
        database_name="r",
    )

    files = [(f"{files_path}/test_{i}.csv", i) for i in range(0, files_to_generate)]
    total_values = generate_random_files(
        started_cluster,
        files_path,
        files_to_generate,
        start_ind=0,
        row_num=1,
        files=files,
    )
    incorrect_values = [
        ["failed", 1, 1],
    ]
    incorrect_values_csv = (
        "\n".join((",".join(map(str, row)) for row in incorrect_values)) + "\n"
    ).encode()

    failed_file = f"{files_path}/testz_fff.csv"
    put_s3_file_content(started_cluster, failed_file, incorrect_values_csv)

    create_mv(node1, f"r.{table_name}", dst_table_name)

    def get_count():
        return int(node1.query(f"SELECT count() FROM default.{dst_table_name}"))

    expected_rows = files_to_generate
    for _ in range(20):
        if expected_rows == get_count():
            break
        time.sleep(1)
    assert expected_rows == get_count()

    create_mv(node2, f"r.{table_name}", dst_table_name)
    for _ in range(20):
        if node2.contains_in_log(f"StorageS3Queue (r.{table_name}): Processed rows: 0"):
            break
        time.sleep(1)
    assert node2.contains_in_log(f"StorageS3Queue (r.{table_name}): Processed rows: 0")

    found_1_global = False
    found_2_global = False
    if mode == "unordered":
        is_unordered = True
    else:
        is_unordered = False

    for file in files:
        found_1 = node2.contains_in_log(
            f"StorageS3Queue (r.{table_name}): Skipping file {file[0]}: Processed"
        )
        found_1_global = found_1_global or found_1

        if is_unordered:
            found_2 = node2.contains_in_log(
                f"Will skip file {file[0]}: it should be processed by"
            )
            found_2_global = found_2_global or found_2
        else:
            found_2 = False

        assert found_1 or found_2, "Failed with file " + file[0]

    assert found_1_global
    if is_unordered:
        assert found_2_global

    assert node2.contains_in_log(
        f"StorageS3Queue (r.{table_name}): Skipping file {failed_file}: Failed"
    ) or node1.contains_in_log(
        f"StorageS3Queue (r.{table_name}): Skipping file {failed_file}: Failed"
    )
