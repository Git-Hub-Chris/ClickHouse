import logging
import os
import time
import warnings

import pymysql
import pytest

from helpers.client import QueryRuntimeException
from helpers.cluster import ClickHouseCluster

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

cluster = ClickHouseCluster(__file__)
node1 = cluster.add_instance(
    "node1",
    with_mysql8=True,
    dictionaries=["configs/dictionaries/simple_dictionary.xml"],
    main_configs=[
        "configs/ssl_conf.xml",
        "configs/client.xml",
        "configs/named_coll.xml",
        "configs/server.crt",
        "configs/server.key",
    ],
    user_configs=["configs/user_admin.xml", "configs/user_default.xml"],
)
node2 = cluster.add_instance(
    "node2",
    with_mysql8=True,
    dictionaries=["configs/dictionaries/simple_dictionary.xml"],
    main_configs=[
        "configs/dictionaries/lazy_load.xml",
        "configs/allow_remote_node.xml",
    ],
    user_configs=["configs/user_admin.xml", "configs/user_default.xml"],
)
node3 = cluster.add_instance(
    "node3",
    main_configs=["configs/allow_remote_node.xml"],
    dictionaries=[
        "configs/dictionaries/dictionary_with_conflict_name.xml",
        "configs/dictionaries/conflict_name_dictionary.xml",
    ],
    user_configs=["configs/user_admin.xml"],
)
node4 = cluster.add_instance(
    "node4", user_configs=["configs/user_admin.xml", "configs/config_password.xml"]
)
node5 = cluster.add_instance(
    "node5",
    # Check that the cluster can start when the dictionary with the bad source query
    # was created using DDL
    clickhouse_path_dir="node5",
    main_configs=["configs/allow_remote_node.xml"],
    dictionaries=["configs/dictionaries/dictionary_with_insert_query.xml"],
    user_configs=["configs/user_admin.xml"],
)


def create_mysql_conn(user, password, hostname, port):
    logging.debug(
        "Created MySQL connection user:{}, host:{}, port{}".format(
            user, hostname, port
        )
    )
    return pymysql.connect(user=user, password=password, host=hostname, port=port)


def execute_mysql_query(connection, query):
    logging.debug("Execute MySQL query:{}".format(query))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with connection.cursor() as cursor:
            cursor.execute(query)
        connection.commit()


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster.start()
        for clickhouse in [node1, node2, node3, node4, node5]:
            clickhouse.query("CREATE DATABASE test", user="admin")
            clickhouse.query(
                "CREATE TABLE test.xml_dictionary_table (id UInt64, SomeValue1 UInt8, SomeValue2 String) ENGINE = MergeTree() ORDER BY id",
                user="admin",
            )
            clickhouse.query(
                "INSERT INTO test.xml_dictionary_table SELECT number, number % 23, hex(number) from numbers(1000)",
                user="admin",
            )
        yield cluster

    finally:
        cluster.shutdown()


@pytest.mark.parametrize(
    "clickhouse,name,layout",
    [
        pytest.param(
            node1,
            "complex_node1_hashed",
            "LAYOUT(COMPLEX_KEY_HASHED())",
            id="complex_node1_hashed",
        ),
        pytest.param(
            node1,
            "complex_node1_cache",
            "LAYOUT(COMPLEX_KEY_CACHE(SIZE_IN_CELLS 10))",
            id="complex_node1_cache",
        ),
        pytest.param(
            node2,
            "complex_node2_hashed",
            "LAYOUT(COMPLEX_KEY_HASHED())",
            id="complex_node2_hashed",
        ),
        pytest.param(
            node2,
            "complex_node2_cache",
            "LAYOUT(COMPLEX_KEY_CACHE(SIZE_IN_CELLS 10))",
            id="complex_node2_cache",
        ),
    ],
)
def test_create_and_select_mysql(started_cluster, clickhouse, name, layout):
    mysql_conn = create_mysql_conn(
        "root", "clickhouse", started_cluster.mysql8_ip, started_cluster.mysql8_port
    )
    execute_mysql_query(mysql_conn, "DROP DATABASE IF EXISTS create_and_select")
    execute_mysql_query(mysql_conn, "CREATE DATABASE create_and_select")
    execute_mysql_query(
        mysql_conn,
        "CREATE TABLE create_and_select.{} (key_field1 int, key_field2 bigint, value1 text, value2 float, PRIMARY KEY (key_field1, key_field2))".format(
            name
        ),
    )
    values = []
    for i in range(1000):
        values.append(
            "(" + ",".join([str(i), str(i * i), str(i) * 5, str(i * 3.14)]) + ")"
        )
    execute_mysql_query(
        mysql_conn,
        "INSERT INTO create_and_select.{} VALUES ".format(name) + ",".join(values),
    )

    clickhouse.query(
        """
    CREATE DICTIONARY default.{} (
        key_field1 Int32,
        key_field2 Int64,
        value1 String DEFAULT 'xxx',
        value2 Float32 DEFAULT '42.42'
    )
    PRIMARY KEY key_field1, key_field2
    SOURCE(MYSQL(
        USER 'root'
        PASSWORD 'clickhouse'
        DB 'create_and_select'
        TABLE '{}'
        REPLICA(PRIORITY 1 HOST '127.0.0.1' PORT 3333)
        REPLICA(PRIORITY 2 HOST 'mysql80' PORT 3306)
    ))
    {}
    LIFETIME(MIN 1 MAX 3)
    """.format(
            name, name, layout
        )
    )

    for i in range(172, 200):
        assert (
            clickhouse.query(
                "SELECT dictGetString('default.{}', 'value1', tuple(toInt32({}), toInt64({})))".format(
                    name, i, i * i
                )
            )
            == str(i) * 5 + "\n"
        )
        stroka = clickhouse.query(
            "SELECT dictGetFloat32('default.{}', 'value2', tuple(toInt32({}), toInt64({})))".format(
                name, i, i * i
            )
        ).strip()
        value = float(stroka)
        assert int(value) == int(i * 3.14)

    for i in range(1000):
        values.append(
            "(" + ",".join([str(i), str(i * i), str(i) * 3, str(i * 2.718)]) + ")"
        )
    execute_mysql_query(
        mysql_conn,
        "REPLACE INTO create_and_select.{} VALUES ".format(name) + ",".join(values),
    )

    clickhouse.query("SYSTEM RELOAD DICTIONARY 'default.{}'".format(name))

    for i in range(172, 200):
        assert (
            clickhouse.query(
                "SELECT dictGetString('default.{}', 'value1', tuple(toInt32({}), toInt64({})))".format(
                    name, i, i * i
                )
            )
            == str(i) * 3 + "\n"
        )
        string = clickhouse.query(
            "SELECT dictGetFloat32('default.{}', 'value2', tuple(toInt32({}), toInt64({})))".format(
                name, i, i * i
            )
        ).strip()
        value = float(string)
        assert int(value) == int(i * 2.718)

    clickhouse.query(
        "select dictGetUInt8('xml_dictionary', 'SomeValue1', toUInt64(17))"
    ) == "17\n"
    clickhouse.query(
        "select dictGetString('xml_dictionary', 'SomeValue2', toUInt64(977))"
    ) == str(hex(977))[2:] + "\n"
    clickhouse.query(f"drop dictionary default.{name}")


def test_restricted_database(started_cluster):
    for node in [node1, node2]:
        node.query("CREATE DATABASE IF NOT EXISTS restricted_db", user="admin")
        node.query(
            "CREATE TABLE restricted_db.table_in_restricted_db AS test.xml_dictionary_table",
            user="admin",
        )

    with pytest.raises(QueryRuntimeException):
        node1.query(
            """
        CREATE DICTIONARY restricted_db.some_dict(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'default' TABLE 'table_in_restricted_db' DB 'restricted_db'))
        LIFETIME(MIN 1 MAX 10)
        """
        )

    with pytest.raises(QueryRuntimeException):
        node1.query(
            """
        CREATE DICTIONARY default.some_dict(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'default' TABLE 'table_in_restricted_db' DB 'restricted_db'))
        LIFETIME(MIN 1 MAX 10)
        """
        )

        node1.query(
            "SELECT dictGetUInt8('default.some_dict', 'SomeValue1', toUInt64(17))"
        ) == "17\n"

    # with lazy load we don't need query to get exception
    with pytest.raises(QueryRuntimeException):
        node2.query(
            """
        CREATE DICTIONARY restricted_db.some_dict(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'default' TABLE 'table_in_restricted_db' DB 'restricted_db'))
        LIFETIME(MIN 1 MAX 10)
        """
        )

    with pytest.raises(QueryRuntimeException):
        node2.query(
            """
        CREATE DICTIONARY default.some_dict(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'default' TABLE 'table_in_restricted_db' DB 'restricted_db'))
        LIFETIME(MIN 1 MAX 10)
        """
        )
    for node in [node1, node2]:
        node.query("DROP DICTIONARY IF EXISTS default.some_dict", user="admin")
        node.query("DROP DATABASE restricted_db", user="admin")


def test_conflicting_name(started_cluster):
    assert (
        node3.query(
            "select dictGetUInt8('test.conflicting_dictionary', 'SomeValue1', toUInt64(17))"
        )
        == "17\n"
    )

    with pytest.raises(QueryRuntimeException):
        node3.query(
            """
        CREATE DICTIONARY test.conflicting_dictionary(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'default' TABLE 'xml_dictionary_table' DB 'test'))
        LIFETIME(MIN 1 MAX 10)
        """
        )

    # old version still works
    node3.query(
        "select dictGetUInt8('test.conflicting_dictionary', 'SomeValue1', toUInt64(17))"
    ) == "17\n"


def test_with_insert_query(started_cluster):
    try:
        node5.query(
            """
        CREATE DICTIONARY test.ddl_dictionary_with_insert_query (
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'default' QUERY 'insert into test.xml_dictionary_table values (234432, 29, "htms")'))
        LIFETIME(MIN 0 MAX 0)
        """
        )
        assert "Invalid dictionary should not be created" and False
    except QueryRuntimeException as ex:
        assert "(SYNTAX_ERROR)" in str(ex)
        assert (
            "Syntax error (Query for ClickHouse dictionary test.ddl_dictionary_with_insert_query)"
            in str(ex)
        )

    try:
        node5.query(
            """
        SELECT dictGet('test.dictionary_with_insert_query', 'SomeValue1', 432234)
        """
        )
    except QueryRuntimeException as ex:
        assert "Only SELECT query can be used as a dictionary source" in str(ex)


def test_http_dictionary_restrictions(started_cluster):
    try:
        node3.query(
            """
        CREATE DICTIONARY test.restricted_http_dictionary (
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(HTTP(URL 'http://somehost.net' FORMAT TabSeparated))
        LIFETIME(1)
        """
        )
        node3.query(
            "SELECT dictGetString('test.restricted_http_dictionary', 'value', toUInt64(1))"
        )
    except QueryRuntimeException as ex:
        assert "is not allowed in configuration file" in str(ex)
    node3.query("DROP DICTIONARY test.restricted_http_dictionary")


def test_file_dictionary_restrictions(started_cluster):
    try:
        node3.query(
            """
        CREATE DICTIONARY test.restricted_file_dictionary (
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(FILE(PATH '/usr/bin/cat' FORMAT TabSeparated))
        LIFETIME(1)
        """
        )
        node3.query(
            "SELECT dictGetString('test.restricted_file_dictionary', 'value', toUInt64(1))"
        )
    except QueryRuntimeException as ex:
        assert "is not inside" in str(ex)
    node3.query("DROP DICTIONARY test.restricted_file_dictionary")


def test_dictionary_with_where(started_cluster):
    mysql_conn = create_mysql_conn(
        "root", "clickhouse", started_cluster.mysql8_ip, started_cluster.mysql8_port
    )
    execute_mysql_query(
        mysql_conn, "CREATE DATABASE IF NOT EXISTS dictionary_with_where"
    )
    execute_mysql_query(
        mysql_conn,
        "CREATE TABLE dictionary_with_where.special_table (key_field1 int, value1 text, PRIMARY KEY (key_field1))",
    )
    execute_mysql_query(
        mysql_conn,
        "INSERT INTO dictionary_with_where.special_table VALUES (1, 'abcabc'), (2, 'qweqwe')",
    )

    node1.query(
        """
    CREATE DICTIONARY default.special_dict (
        key_field1 Int32,
        value1 String DEFAULT 'xxx'
    )
    PRIMARY KEY key_field1
    SOURCE(MYSQL(
        USER 'root'
        PASSWORD 'clickhouse'
        DB 'dictionary_with_where'
        TABLE 'special_table'
        REPLICA(PRIORITY 1 HOST 'mysql80' PORT 3306)
        WHERE 'value1 = \\'qweqwe\\' OR value1 = \\'\\\\u3232\\''
    ))
    LAYOUT(FLAT())
    LIFETIME(MIN 1 MAX 3)
    """
    )

    node1.query("SYSTEM RELOAD DICTIONARY default.special_dict")

    assert (
        node1.query(
            "SELECT dictGetString('default.special_dict', 'value1', toUInt64(2))"
        )
        == "qweqwe\n"
    )
    node1.query("DROP DICTIONARY default.special_dict")
    execute_mysql_query(mysql_conn, "DROP TABLE dictionary_with_where.special_table")
    execute_mysql_query(mysql_conn, "DROP DATABASE dictionary_with_where")


def test_clickhouse_remote(started_cluster):
    with pytest.raises(QueryRuntimeException):
        node3.query(
            """
        CREATE DICTIONARY test.clickhouse_remote(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'node4' PORT 9000 USER 'default' TABLE 'xml_dictionary_table' DB 'test'))
        LIFETIME(MIN 1 MAX 10)
        """
        )
        for i in range(5):
            node3.query("system reload dictionary test.clickhouse_remote")
            time.sleep(0.5)

    node3.query("detach dictionary if exists test.clickhouse_remote")

    with pytest.raises(QueryRuntimeException):
        node3.query(
            """
            CREATE DICTIONARY test.clickhouse_remote(
                id UInt64,
                SomeValue1 UInt8,
                SomeValue2 String
            )
            PRIMARY KEY id
            LAYOUT(FLAT())
            SOURCE(CLICKHOUSE(HOST 'node4' PORT 9000 USER 'default' PASSWORD 'default' TABLE 'xml_dictionary_table' DB 'test'))
            LIFETIME(MIN 1 MAX 10)
            """
        )

    node3.query("attach dictionary test.clickhouse_remote")
    node3.query("drop dictionary test.clickhouse_remote")

    node3.query(
        """
        CREATE DICTIONARY test.clickhouse_remote(
            id UInt64,
            SomeValue1 UInt8,
            SomeValue2 String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(HOST 'node4' PORT 9000 USER 'default' PASSWORD 'default' TABLE 'xml_dictionary_table' DB 'test'))
        LIFETIME(MIN 1 MAX 10)
        """
    )

    node3.query(
        "select dictGetUInt8('test.clickhouse_remote', 'SomeValue1', toUInt64(17))"
    ) == "17\n"


# https://github.com/ClickHouse/ClickHouse/issues/38450
#  suggests placing 'secure' in named_collections
def test_secure(started_cluster):
    node1.query("DROP TABLE IF EXISTS test.foo_dict")
    node1.query("CREATE TABLE test.foo_dict(`id` UInt64, `value` String) ENGINE = Log")
    node1.query("INSERT INTO test.foo_dict values (1, 'value1')")

    # No named collection, secure is set in DDL
    node1.query("DROP DICTIONARY IF EXISTS test.clickhouse_secure")
    node1.query(
        """
        CREATE DICTIONARY test.clickhouse_secure(
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(
            HOST 'localhost'
            PORT 9440 USER 'default'
            TABLE 'foo_dict' DB 'test'
            SECURE 1
            ))
        LIFETIME(MIN 1 MAX 10)
            """
    )
    value = node1.query(
        "SELECT dictGet('test.clickhouse_secure', 'value', toUInt64(1))"
    )
    assert value == "value1\n"

    # Secure set in named collection
    node1.query("DROP DICTIONARY IF EXISTS test.clickhouse_secure")
    node1.query(
        """
        CREATE DICTIONARY test.clickhouse_secure(
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(
            NAME 'nc_secure_1'
            TABLE 'foo_dict' DB 'test'
            ))
        LIFETIME(MIN 1 MAX 10)
            """
    )
    value = node1.query(
        "SELECT dictGet('test.clickhouse_secure', 'value', toUInt64(1))"
    )
    assert value == "value1\n"

    # Secure is not set
    node1.query("DROP DICTIONARY IF EXISTS test.clickhouse_secure")
    node1.query(
        """
        CREATE DICTIONARY test.clickhouse_secure(
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(
            NAME 'nc_no_secure'
            TABLE 'foo_dict' DB 'test'
            ))
        LIFETIME(MIN 1 MAX 10)
            """
    )
    with pytest.raises(QueryRuntimeException) as excinfo:
        node1.query("SELECT dictGet('test.clickhouse_secure', 'value', toUInt64(1))")
    assert "Unexpected packet from server localhost:9440" in str(excinfo.value)

    # Secure is set to 0 in named collection
    node1.query("DROP DICTIONARY IF EXISTS test.clickhouse_secure")
    node1.query(
        """
        CREATE DICTIONARY test.clickhouse_secure(
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(
            NAME 'nc_secure_0'
            TABLE 'foo_dict' DB 'test'
            ))
        LIFETIME(MIN 1 MAX 10)
            """
    )
    with pytest.raises(QueryRuntimeException) as excinfo:
        node1.query("SELECT dictGet('test.clickhouse_secure', 'value', toUInt64(1))")
    assert "Unexpected packet from server localhost:9440" in str(excinfo.value)

    # Secure is set to 0 in named collection and in 1 in DDL
    node1.query("DROP DICTIONARY IF EXISTS test.clickhouse_secure")
    node1.query(
        """
        CREATE DICTIONARY test.clickhouse_secure(
            id UInt64,
            value String
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(
            NAME 'nc_secure_0'
            TABLE 'foo_dict' DB 'test'
            SECURE 1
            ))
        LIFETIME(MIN 1 MAX 10)
            """
    )
    value = node1.query(
        "SELECT dictGet('test.clickhouse_secure', 'value', toUInt64(1))"
    )
    assert value == "value1\n"

    node1.query("DROP DICTIONARY test.clickhouse_secure")
    node1.query("DROP TABLE test.foo_dict")


def test_named_collection(started_cluster):
    node1.query(
        """
        CREATE DICTIONARY test.clickhouse_named_collection(
            id UInt64,
            SomeValue1 UInt8
        )
        PRIMARY KEY id
        LAYOUT(FLAT())
        SOURCE(CLICKHOUSE(NAME click1))
        LIFETIME(MIN 1 MAX 10)
        """
    )

    node1.query(
        "select dictGetUInt8('test.clickhouse_named_collection', 'SomeValue1', toUInt64(23))"
    ) == "0\n"

    node1.query("DROP DICTIONARY test.clickhouse_named_collection")
