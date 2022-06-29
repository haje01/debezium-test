"""

공용 유틸리티

"""
import os
import json
from json import JSONDecodeError
import re
import time
import binascii
import subprocess

import pymssql
from mysql.connector import connect
from faker import Faker
from faker.providers import internet, date_time, company, phone_number
import paramiko
import boto3
import pytest
from retry import retry

# MSSQL의 경우 사전 100000 행이 들어가 있으면 Insert / Select 성능이 25% 정도 감소
# MySQL 은 Select 만 10% 정도 감소
# (AWS I/O Bound 일수도)
DB_PRE_EPOCH = 0
DB_PRE_BATCH = 1000
DB_PRE_ROWS = DB_PRE_EPOCH * DB_PRE_BATCH

# Kafka 내장 토픽 이름
INTERNAL_TOPICS = ['__consumer_offsets', 'connect-configs', 'connect-offsets', 'connect-status']
DB_PORTS = {'mysql': 3306, 'mssql': 1433}
HOME = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def db_port(profile):
    return DB_PORTS[profile]


def insert_fake(conn, cursor, epoch, batch, pid, profile):
    """Fake 데이터를 DB insert."""
    assert profile in ('mysql', 'mssql')

    fake = Faker()
    fake.add_provider(internet)
    fake.add_provider(date_time)
    fake.add_provider(company)
    fake.add_provider(phone_number)

    if profile == 'mysql':
        sql = "INSERT INTO person(pid, sid, name, address, ip, birth, company, phone) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)"
    else:
        sql = "INSERT INTO person VALUES(%s, %s, %s, %s, %s, %s, %s, %s)"

    for j in range(epoch):
        print(f"Inserter {pid} epoch: {j+1}")
        rows = []
        for i in range(batch):
            row = (
                pid,
                j * batch + i,
                fake.name(),
                fake.address(),
                fake.ipv4_public(),
                fake.date(),
                fake.company(),
                fake.phone_number()
            )
            rows.append(row)
        cursor.executemany(sql, rows)
        conn.commit()


@retry(RuntimeError, tries=10, delay=3)
def SSH(host, name=None):
    """Paramiko SSH 접속 생성."""
    name = host if name is None else f'{name} ({host})'
    print(f"[ ] ssh connect {name}")
    ssh_pkey = os.environ['KFKTEST_SSH_PKEY']
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.RSAKey.from_private_key_file(ssh_pkey)
    try:
        ssh.connect(host, username='ubuntu', pkey=pkey)
    except paramiko.ssh_exception.NoValidConnectionsError as e:
        raise RuntimeError(f"Can not connnect to {host}")
    print(f"[v] ssh connect {name}")
    return ssh


def ssh_exec(ssh, cmd, kafka_env=True, stderr_type="stderr", ignore_err=False):
    """SSH 로 명령 실행

    Args:
        ssh: 명령을 실행할 Paramiko SSH 객체
        cmd (str): 명령
        kafka_env (bool): 카프카 환경 변수 설정 여부. 기본 True
        stderr_type (str): 표준 에러 출력을 어떻게 다룰 것인지
            - stderr: 에러 출력 (기본)
            - stdout: 표준 출력
            - ignore: 무시
        ignore_err (bool): 에러가 있어도 무시

    """
    env = "source ~/.kenv && " if kafka_env else ""
    cmd = f'{env}{cmd}'
    _, stdout, stderr = ssh.exec_command(cmd)
    es = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf8')
    err = stderr.read().decode('utf8')
    if stderr_type == "stdout":
        out = err
        err = ''
    elif stderr_type == "ignore":
        err = ''
    elif es != 0 and err == '':
        err = out
    if es != 0 and not ignore_err :
        if err != '':
            ssh_addr = ssh.get_transport().getpeername()[0]
            raise Exception(f'[@{ssh_addr}] {cmd} <--- {err}')
    return out


def local_exec(cmd):
    """로컬에서 쉘 명령 실행."""
    return subprocess.run(cmd, shell=True)


def scp_to_remote(src, dst_addr, dst_dir):
    """로컬 파일을 원격지로 scp.

    Args:
        src (str): 원본 파일 경로
        dst_addr (str): 대상 노드 주소
        dst_dir (str): 대상 디렉토리

    """
    print(f'scp_to_remote: {src} to {dst_addr}:{dst_dir}')
    assert not dst_dir.endswith('/')
    ssh_pkey = os.environ['KFKTEST_SSH_PKEY']

    dst_ssh = SSH(dst_addr)
    # 대상 디렉토리 확보
    ssh_exec(dst_ssh, f'mkdir -p {dst_dir}', False)
    # scp
    src = os.path.abspath(src)
    cmd = f'scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '\
        f'-i {ssh_pkey} {src} ubuntu@{dst_addr}:{dst_dir}'
    return local_exec(cmd)


def list_topics(kfk_ssh, skip_internal=True):
    """토픽 리스팅.

    Args:
        profile: 프로파일 명
        skip_internal: 내부 토픽 생략 여부. 기본 True

    Returns:
        list: 토픽 이름 리스트

    """
    print("list_topics at kafka")
    ret = ssh_exec(kfk_ssh, f'kafka-topics.sh --list --bootstrap-server localhost:9092')
    topics = ret.strip().split('\n')
    if skip_internal:
        topics = [t for t in topics if t not in INTERNAL_TOPICS]
    return topics


def create_topic(kfk_ssh, topic, partitions=12, replications=1):
    """토픽 생성.

    Args:
        kfk_ssh : Kafka 노드의 Paramiko SSH 객체
        topic (str): 생성할 토픽 이름

    """
    print(f"[ ] create_topic '{topic}'")
    ret = ssh_exec(kfk_ssh, f'kafka-topics.sh --create --topic {topic} --bootstrap-server localhost:9092 --partitions {partitions} --replication-factor {replications}')
    print(f"[v] create_topic '{topic}'")
    return ret



def claim_topic(kfk_ssh, topic, partitions=12, replications=1):
    """토픽이 없는 경우만 생성.

    Args:
        kfk_ssh : Kafka 노드의 Paramiko SSH 객체
        topic (str): 생성할 토픽 이름

    """
    print(f"[ ] claim_topic: {topic} at kafka")
    topics = list_topics(kfk_ssh)
    if topic not in topics:
        create_topic(kfk_ssh, topic, partitions=partitions, replications=replications)
    print(f"[v] claim_topic: {topic} at kafka")


def describe_topic(kfk_ssh, topic):
    """토픽 정보 얻기.

    Returns:
        tuple: 토픽 정보, 토픽 파티션들 정보

    """
    ret = ssh_exec(kfk_ssh, f'kafka-topics.sh --describe --topic {topic} --bootstrap-server localhost:9092')
    items = ret.strip().split('\n')
    items = [item.split('\t') for item in items]
    return items[0], items[1:]


def get_kafka_ssh(profile):
    """프로파일에 맞는 Kafka SSH 얻기."""
    setup = load_setup(profile)
    ip = setup['kafka_public_ip']['value']
    ssh = SSH(ip, 'kafka')
    return ssh


@pytest.fixture
def xkfssh(xprofile, xkvmstart):
    print("xkfssh")
    ssh = get_kafka_ssh(xprofile)
    yield ssh


def check_topic_exists(kfk_ssh, topic):
    """토픽 존재 여부를 체크."""
    return _check_topic_exists(kfk_ssh, topic)


def _check_topic_exists(kfk_ssh, topic):
    ret = ssh_exec(kfk_ssh, "kafka-topics.sh --list --bootstrap-server localhost:9092")
    topics = ret.strip().split('\n')
    return topic in topics


@retry(RuntimeError, tries=7, delay=3)
def delete_topic(kfk_ssh, topic, ignore_not_exist=False):
    """토픽 삭제.

    토픽이 가끔 삭제되지 않는 이슈:
    - 프로듀서/컨슈머가 이용중인 토픽을 지우려고 할 때 발생하는 듯
    - 가급적 테스트 종료 후 충분한 시간이 지난 뒤 (재시작시 등) 지울 것
    - 문제 발생시 zookeeper shell 로 지워주어야

    Args:
        kfk_ssh : Kafka 노드의 Paramiko SSH 객체
        topic (str): 삭제할 토픽 이름
        ignore_not_exist (bool): 토픽이 존재하지 않는 경우 에러 무시
        retry (int): 삭제가 완료될 때까지 재시도 횟수. 기본값 None

    """

    print(f"[ ] delete_topic '{topic}'")

    try:
        ret = ssh_exec(kfk_ssh, f'kafka-topics.sh --delete --topic {topic}  --bootstrap-server localhost:9092')
    except Exception as e:
        if 'does not exist' in str(e) and ignore_not_exist:
            print(f"   delete_topic '{topic}' - not exist")
            return
        else:
            raise e

    if _check_topic_exists(kfk_ssh, topic):
        raise RuntimeError(f"Topic {topic} still remain.")
    print(f"[v] delete_topic '{topic}'")
    return ret


def delete_all_topics(profile):
    """내장 토픽을 제외한 모든 토픽 제거."""
    topics = list_topics(profile)
    for topic in topics:
        delete_topic(profile, topic)


def reset_topic(profile, topic, partitions=12, replications=1):
    """특정 토픽 초기화."""
    print(f"[ ] reset_topic '{topic}'")
    delete_topic(profile, topic, True)
    create_topic(profile, topic, partitions, replications)
    print(f"[v] reset_topic '{topic}'")


def count_topic_message(kfk_ssh, topic, from_begin=True, timeout=10):
    """토픽의 메시지 수를 카운팅.

    카프카 커넥트가 제대로 동작하지 않는 경우 계속 같은 수가 나와 조기 종료되는
    문제 있음. 이럴 때는 카프카 커넥트를 재시작해야 한다

    Args:
        kfk_ssh : Kafka 노드의 Paramiko SSH 객체
        topic (str): 토픽명
        from_begin (bool): 토픽의 첫 메시지부터. 기본값 True
        timeout (int): 컨슘 타임아웃 초. 기본값 10초
            너무 작은 값이면 카운팅이 끝나기 전에 종료될 수 있다.

    """
    print(f"[ ] count_topic_message - topic: {topic}, timeout: {timeout}")
    cmd = f'''kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic {topic} --timeout-ms {timeout * 1000}'''
    if from_begin:
        cmd += ' --from-beginning'
    cmd += ' | tail -n 10 | grep Processed'
    ret = ssh_exec(kfk_ssh, cmd, stderr_type="stdout")
    msg = ret.strip().split('\n')[-1]
    match = re.search(r'Processed a total of (\d+) messages', msg)
    if match is not None:
        cnt = int(match.groups()[0])
    else:
        raise Exception(f"Not matching result: {msg}")
    print(f"[v] count_topic_message - topic: {topic}, timeout: {timeout}")
    return cnt


@retry(RuntimeError, tries=6, delay=5)
def register_jdbc(kfk_ssh, profile, db_addr,
        db_port, db_user, db_passwd, db_name, tables, topic_prefix,
        poll_interval=5000):
    """카프카 JDBC Source 커넥터 등록.

    설정에 관한 참조:
        https://www.confluent.io/blog/kafka-connect-deep-dive-jdbc-source-connector/

    Args:
        kfk_ssh: Paramiko SSH 객체
        profile (str): 커넥션 URL 용 DBMS 타입. mysql 또는 mssql
        db_addr (str): DB 주소 (Private IP)
        db_port (int): DB 포트
        db_name (str): 사용할 DB 이름
        db_user (str): DB 유저
        db_passwd (str): DB 유저 암호
        tables (str): 대상 테이블 이름. 하나 이상인 경우 ',' 로 구분
        topic_prefix (str): 테이블을 넣을 토픽의 접두사
        poll_interval (int): ms 단위 폴링 간격. 기본값 5000

    """
    assert profile in ('mysql', 'mssql')
    if profile == 'mssql':
        db_url = f"jdbc:sqlserver://{db_addr}:{db_port};databaseName={db_name};encrypt=true;trustServerCertificate=true;"
    else:
        db_url = f"jdbc:mysql://{db_addr}:{db_port}/{db_name}"

    hash = binascii.hexlify(os.urandom(3)).decode('utf8')
    conn_name = f'jdbc-{profile}-{hash}'
    print(f"[ ] register_jdbc {conn_name}: {db_url}")
    isolation = 'READ_UNCOMMITTED' if profile == 'mssql' else 'DEFAULT'

    config = f'''{{
        "connector.class" : "io.confluent.connect.jdbc.JdbcSourceConnector",
        "connection.url":"{db_url}",
        "connection.user":"{db_user}",
        "connection.password":"{db_passwd}",
        "auto.create": false,
        "auto.evolve": false,
        "poll.interval.ms": 5000,
        "table.poll.interval.ms": 60000,
        "delete.enabled": true,
        "transaction.isolation.mode": "{isolation}",
        "mode": "incrementing",
        "incrementing.column.name" : "id",
        "table.whitelist": "{tables}",
        "topic.prefix" : "{topic_prefix}",
        "poll.interval.ms": "{poll_interval}",
        "tasks.max" : "1"
    }}'''
    data = _register_connector(kfk_ssh, conn_name, config)
    print(f"[v] register_jdbc {profile} for {tables}")
    return data


@retry(RuntimeError, tries=6, delay=5)
def register_dbzm(kfk_ssh, profile, svr_name, db_addr, db_port, db_name,
        db_user, db_passwd):
    """Debezium MySQL/MSSQL 커넥터 등록

    설정에 관한 참조:
        https://debezium.io/documentation/reference/1.9/connectors/mysql.html
        https://debezium.io/documentation/reference/1.9/connectors/sqlserver.html
        https://debezium.io/blog/2020/09/03/debezium-1-3-beta1-released/

    Args:
        kfk_ssh: Paramiko SSH 객체
        profile (str): 프로파일 명
        svr_name (str): DB 서버 명
        db_addr (str): DB 주소 (Private IP)
        db_port (int): DB 포트
        db_name (str): 대상 DB 이름
        db_user (str): DB 유저
        db_passwd (str): DB 유저 암호

    """
    hash = binascii.hexlify(os.urandom(3)).decode('utf8')
    conn_name = f'dbzm-{profile}-{hash}'
    print(f"[ ] register_dbzm {conn_name} for {db_name}")

    # 공통 설정
    config = {
        "database.hostname": db_addr,
        "database.port": f"{db_port}",
        "database.user": db_user,
        "database.password": db_passwd,
        "database.server.name": svr_name,
        "database.history.kafka.bootstrap.servers": "localhost:9092",
        "database.history.kafka.topic": f"{profile}.history.{svr_name}",
        "include.schema.changes": "true",
        "tasks.max": "1"
    }

    if profile == 'mysql':
        cls_name = 'mysql.MySqlConnector'
        config["database.include.list"] = db_name
        config["database.server.id"] = "1"
    else:
        cls_name = 'sqlserver.SqlServerConnector'
        config['database.dbname'] = db_name

    config['connector.class'] = f"io.debezium.connector.{cls_name}"

    # config = f'''{{
    #     "connector.class":"io.debezium.connector.{cls_name}",
    #     "database.hostname":"{db_addr}",
    #     "database.port":"{db_port}",
    #     "database.user":"{db_user}",
    #     "database.password":"{db_passwd}",
    #     "database.server.id":"1",
    #     "database.server.name":"{svr_name}",
    #     "database.include.list":"{db_name}",
    #     "database.history.kafka.bootstrap.servers":"localhost:9092",
    #     "database.history.kafka.topic":"{profile}.history.{svr_name}",
    #     "include.schema.changes": "true",
    #     "tasks.max": "1"
    # }}'''

    data = _register_connector(kfk_ssh, conn_name, json.dumps(config))
    # 등록된 커넥터 상태 확인
    time.sleep(5)
    status = get_connector_status(kfk_ssh, conn_name)
    if status['tasks'][0]['state'] == 'FAILED':
        raise RuntimeError(status['tasks'][0]['trace'])
    print(f"[v] register_dbzm {conn_name} for {db_name}")
    return data


def get_connector_status(kfk_ssh, conn_name):
    """등록된 카프카 커넥터 상태를 얻음."""
    print(f"[ ] get_connector_status")
    cmd = f'''curl -s http://localhost:8083/connectors/{conn_name}/status'''
    ret = ssh_exec(kfk_ssh, cmd)
    status = json.loads(ret)
    return status


def _register_connector(kfk_ssh, name, config, poll_interval=5000):
    """공용 카프카 커넥터 등록.

    Args:
        kfk_ssh: Kafka 노드 Paramiko SSH 객체
        name (str): 커넥터 이름
        config (str): 커넥터 설정
        poll_interval (int): ms 단위 폴링 간격. 기본값 5000

    """
    cmd = f'''
curl -vs -X POST 'http://localhost:8083/connectors' \
    -H 'Content-Type: application/json' \
    --data-raw '{{ \
    "name" : "{name}", \
    "config" : {config} \
}}' '''

    ret = ssh_exec(kfk_ssh, cmd)
    try:
        data = json.loads(ret)
    except Exception as e:
        msg = str(e)
        print(msg)
        raise RuntimeError(msg)
    return data


@retry(RuntimeError, tries=6, delay=5)
def list_socons(kfk_ssh):
    """등록된 카프카 Source 커넥터 리스트.

    Returns:
        list: 등록된 커넥터 이름 리스트

    """
    print(f"[ ] list_socons")
    cmd = f'''curl -s http://localhost:8083/connectors'''
    ret = ssh_exec(kfk_ssh, cmd)
    try:
        conns = json.loads(ret)
    except json.decoder.JSONDecodeError as e:
        raise RuntimeError(str(e))
    print(f"[v] list_socons")
    return conns


@retry(RuntimeError, tries=6, delay=5)
def unregister_socon(kfk_ssh, conn_name):
    """카프카 JDBC Source 커넥터 해제.

    Args:
        kfk_ssh : Kafka 노드의 Paramiko SSH 객체
        conn_name (str): 커넥터 이름

    """
    print(f"[ ] unregister_socon {conn_name}")
    cmd = f'''curl -X DELETE http://localhost:8083/connectors/{conn_name}'''
    try:
        ssh_exec(kfk_ssh, cmd, ignore_err=True)
    except json.decoder.JSONDecodeError as e:
        raise RuntimeError(str(e))
    print(f"[v] unregister_socon {conn_name}")


def unregister_all_socons(kfk_ssh):
    """등록된 모든 카프카 Source 커넥터 해제."""
    print("[ ] unregister_all_socons")
    for socon in list_socons(kfk_ssh):
        unregister_socon(kfk_ssh, socon)
    print("[v] unregister_all_socons")


@retry(RuntimeError, tries=6, delay=5)
def start_zookeeper(kfk_ssh):
    """주키퍼 시작."""
    print(f"[ ] start_zookeeper")
    ssh_exec(kfk_ssh, "sudo systemctl start zookeeper")
    # 시간 경과 후에 동작 확인
    time.sleep(5)
    # 브로커 시작 확인
    if not is_service_active(kfk_ssh, 'zookeeper'):
        raise RuntimeError('Zookeeper not launched!')
    print(f"[v] start_zookeeper")


@retry(RuntimeError, tries=6, delay=5)
def stop_zookeeper(kfk_ssh, ignore_err=False):
    """주키퍼 정지."""
    print(f"[ ] stop_zookeeper")
    ssh_exec(kfk_ssh, "sudo systemctl stop zookeeper", ignore_err=ignore_err)
    print(f"[v] stop_zookeeper")


@retry(RuntimeError, tries=6, delay=5)
def start_kafka_broker(kfk_ssh):
    """카프카 브로커 시작.

    주: 프로세스 kill 후 몇 번 시도가 필요한 듯.

    """
    print(f"[ ] start_kafka_broker")
    ssh_exec(kfk_ssh, "sudo systemctl start kafka")
    # 충분한 시간 경과 후에 동작을 확인해야 한다.
    # kill 시 Zookeeper 의 기존 /brokers/ids/0 겹치는 문제로 기동중 다시 죽을 수 있음
    time.sleep(15)
    # 브로커 시작 확인
    if not is_service_active(kfk_ssh, 'kafka'):
        raise RuntimeError('Broker not launched!')
    print(f"[v] start_kafka_broker")


def is_service_active(ssh, svc_name):
    """systemctl 을 이용해 서비스 Active 상태를 얻음."""
    ret = ssh_exec(ssh, f"systemctl status {svc_name} | grep Active | awk '{{print $2}}'")
    stat = ret.strip()
    print(f"is_service_active - {svc_name} {stat}")
    return stat == 'active'


def stop_kafka_broker(kfk_ssh, ignore_err=False):
    """카프카 브로커 정지.

    주: 의존성에 따라 Kafka Connect 를 먼저 멈추고 이것을 불러야 한다.

    """
    print(f"[ ] stop_kafka_broker")
    ssh_exec(kfk_ssh, "sudo systemctl stop kafka", ignore_err=ignore_err)
    # ssh_exec(kfk_ssh, "sudo $KAFKA_HOME/bin/kafka-server-stop.sh", ignore_err=ignore_err)
    print(f"[v] stop_kafka_broker")


@pytest.fixture
def xkvmstart(xprofile):
    """Kafka VM이 정지상태이면 재개."""
    print("xkvmstart")
    claim_vm_start(xprofile, 'kafka')
    yield


def claim_vm_start(profile, inst_name):
    """VM 실행 확인."""
    inst = ec2inst_by_name(profile, inst_name)
    state = inst.state['Name']
    if state != 'running':
        vm_start(profile, inst_name)


@pytest.fixture
def xzookeeper(xsetup, xkfssh):
    """주키퍼 동작 확인."""
    print("xzookeeper")
    claim_zookeeper(xkfssh)
    yield


@retry(RuntimeError, tries=10, delay=3)
def claim_zookeeper(kfk_ssh):
    """주키퍼 동작 확인."""
    print("[ ] claim_zookeeper")
    ret = ssh_exec(kfk_ssh, 'fuser 2181/tcp', stderr_type='stdout')
    # 주키퍼가 떠있지 않으면 시작
    if ret == '':
        start_zookeeper(xkfssh)
    print("[v] claim_zookeeper")


@pytest.fixture
def xkafka(xsetup, xkfssh, xzookeeper):
    """카프카 브로커 동작 확인."""
    print("xkafka")
    claim_kafka(xkfssh)
    yield


def claim_kafka(kfk_ssh):
    """카프카 브로커 동작 확인."""
    print("[ ] claim_kafka")
    # 카프카가 떠있지 않으면 시작
    if not is_service_active(kfk_ssh, 'kafka'):
        start_kafka_broker(kfk_ssh)
    print("[v] claim_kafka")


@pytest.fixture
def xtopic_ct(xkfssh, xprofile, xkafka):
    """CT 테스트용 카프카 토픽 초기화."""
    print("xtopic_ct")
    reset_topic(xkfssh, f"{xprofile}-person")
    yield


@pytest.fixture
def xtopic_cdc(xkfssh, xprofile, xkafka):
    """CDC 테스트용 카프카 토픽 초기화."""
    print("xtopic_cdc")
    reset_topic(xkfssh, f"db1")
    scm = 'dbo' if xprofile == 'mssql' else 'test'
    reset_topic(xkfssh, f"db1.{scm}.person")
    yield


def setup_path(profile):
    return f'../temp/{profile}/setup.json'


@pytest.fixture(scope="session")
def xsetup(xprofile):
    """인프라 설치 정보."""
    print("xsetup")
    assert os.path.isfile(setup_path(xprofile))
    with open(setup_path(xprofile), 'rt') as f:
        return json.loads(f.read())


@pytest.fixture(scope="session")
def xcp_setup(xprofile, xsetup):
    """확보된 인프라 설치 정보를 원격 노드에 복사."""
    print("xcp_setup")
    from kfktest.cpsetup import cp_setup as _cp_setup
    _cp_setup(xprofile)
    yield xsetup


@pytest.fixture
def xdbconcur(xprofile):
    print("xdbconcur")
    conn, cursor = db_concur(xprofile)
    yield conn, cursor
    conn.close()


def db_concur(profile):
    """프로파일에 적합한 DB 커넥션과 커서 얻기."""
    setup = load_setup(profile)
    db_addr = setup[f'{profile}_public_ip']['value']
    db_user = setup['db_user']['value']
    db_passwd = setup['db_passwd']['value']['result']
    if profile == 'mysql':
        conn = connect(host=db_addr, user=db_user, password=db_passwd, database="test")
    else:
        conn = pymssql.connect(host=db_addr, user=db_user, password=db_passwd, database="test")
    cursor = conn.cursor()
    return conn, cursor


def mysql_exec_many(cursor, stmt):
    """MySQL 용 멀티 라인 쿼리 실행

    주: 결과를 읽어와야 쿼리 실행이 됨

    """
    results = cursor.execute(stmt, multi=True)
    for res in results:
        print(res)


@pytest.fixture
def xtable(xprofile, xkafka):
    """테스트용 테이블 초기화."""
    print("xtable")
    from kfktest.table import reset_table

    conn, cursor = reset_table(xprofile)

    from kfktest.inserter import insert
    if DB_PRE_ROWS > 0 :
        print("[ ] insert initial data")
        insert(db_type=xprofile, epoch=DB_PRE_EPOCH, batch=DB_PRE_BATCH, pid=1, dev=True, show_result=False)
        print("[v] insert initial data")

    yield


@pytest.fixture
def xconn(xkfssh, xsetup):
    """테스트용 카프카 커넥트 기동 요청.

    - 커넥트가 기동되지 않았으면 기동
    - VM 정지 후 재시작하면 Kafka Connect 가 비정상 -> 재시작해야

    """
    print("xconn")
    claim_kafka_connect(xkfssh)
    yield


def claim_kafka_connect(kfk_ssh):
    """카프카 커넥트 요청."""
    print("[ ] claim_kafka_connect")
    if not is_service_active(kfk_ssh, 'kafka-connect'):
        start_kafka_connect(kfk_ssh)
    print("[v] claim_kafka_connect")


@retry(RuntimeError, tries=6, delay=5)
def start_kafka_connect(kfk_ssh):
    """카프카 커넥트 시작."""
    print(f"[ ] start_kafka_connect")
    ssh_exec(kfk_ssh, "sudo systemctl start kafka-connect")
    # 잠시 후 동작 확인
    time.sleep(5)
    # 시작 확인
    if not is_service_active(kfk_ssh, 'kafka-connect'):
        raise RuntimeError('Connect not launched!')
    print(f"[v] start_kafka_connect")


def start_kafka_and_connect(kfk_ssh):
    """의존성을 고려해 Kafka 와 Kafka Connect 시작."""
    start_kafka_broker(kfk_ssh)
    start_kafka_connect(kfk_ssh)


def stop_kafka_and_connect(kfk_ssh):
    """의존성을 고려해 Kafka 와 Kafka Connect 정지."""
    stop_kafka_connect(kfk_ssh)
    stop_kafka_broker(kfk_ssh)


def stop_kafka_connect(kfk_ssh):
    """카프카 커넥트 정지."""
    print(f"[ ] stop_kafka_connect")
    ssh_exec(kfk_ssh, "sudo kill $(fuser 8083/tcp)")
    print(f"[v] stop_kafka_connect")


@pytest.fixture
def xrmcons(xkfssh):
    """등록된 모든 Source Connector 제거."""
    unregister_all_socons(xkfssh)


@pytest.fixture
def xsocon(xprofile, xrmcons, xkfssh, xtable, xtopic_ct, xconn, xsetup):
    """CT용 Kafka 소스 커넥터 초기화 (테이블과 토픽 먼저 생성)."""
    print("xsocon")
    db_addr = xsetup[f'{xprofile}_private_ip']['value']
    db_user = xsetup['db_user']['value']
    db_passwd = xsetup['db_passwd']['value']['result']

    ret = register_jdbc(xkfssh, xprofile, db_addr, DB_PORTS[xprofile],
        db_user, db_passwd, "test", "person", f"{xprofile}-")
    time.sleep(5)
    yield

    # unregister_socon(xprofile, conn_name)


@pytest.fixture
def xdbzm(xprofile, xkfssh, xrmcons, xtopic_cdc, xconn, xsetup, xcdc):
    """CDC 용 Debezium Source 커넥터 초기화 (테이블과 토픽 먼저 생성)."""
    print("xdbzm")
    db_addr = xsetup[f'{xprofile}_private_ip']['value']
    db_user = xsetup['db_user']['value']
    db_passwd = xsetup['db_passwd']['value']['result']
    svr_name = "db1"

    ret = register_dbzm(xkfssh, xprofile, svr_name, db_addr,
        DB_PORTS[xprofile], "test", db_user, db_passwd)
    if 'error_code' in ret:
        raise RuntimeError(ret['message'])
    time.sleep(5)
    yield

    # unregister_socon(xprofile, conn_name)


def load_setup(profile):
    """Profile 에 맞는 setup.json 읽기.

    snakemake -f temp/{profile}/setup.json 로 미리 만들어져야 함.

    """
    path = os.path.join(HOME, 'temp', profile, 'setup.json')
    with open(path, 'rt') as f:
        return json.loads(f.read())


def kill_proc_by_port(ssh, port):
    """원격 노드의 프로세스를 열린 포트 번호로 강제 kill.

    Args:
        ssh: 원격 노드의 Paramiko SSH 객체
        port: 대상 포트

    """
    print(f"[ ] kill_proc_by_port {port}")
    cmd = f'kill -9 $(fuser {port}/tcp)'
    ssh_exec(ssh, cmd, stderr_type='stdout')
    print(f"[v] kill_proc_by_port {port}")


def ec2inst_by_name(profile, inst_name):
    setup = load_setup(profile)
    key = f'{inst_name}_instance_id'
    inst_id = setup[key]['value']
    ec2 = boto3.resource('ec2')
    inst = ec2.Instance(inst_id)
    return inst


def vm_stop(profile, inst_name):
    """VM 정지."""
    print(f"[ ] vm_stop {inst_name}")
    inst = ec2inst_by_name(profile, inst_name)
    inst.stop()
    inst.wait_until_stopped()
    # wait_vm_state(profile, inst_name, 'stopped')
    print(f"[v] vm_stop {inst_name}")


def vm_hibernate(profile, inst_name):
    """VM Hibernate."""
    print(f"[ ] vm_hibernate {inst_name} of {profile}")
    inst = ec2inst_by_name(profile, inst_name)
    inst.stop(Hibernate=True)
    inst.wait_until_stopped()
    # wait_vm_state(profile, inst_name, 'running')
    print(f"[v] vm_hibernate {inst_name} of {profile}")


def vm_start(profile, inst_name):
    """VM 시작."""
    print(f"[ ] vm_start {inst_name} of {profile}")
    inst = ec2inst_by_name(profile, inst_name)
    inst.start()
    inst.wait_until_running()
    # wait_vm_state(profile, inst_name, 'running')
    print(f"[v] vm_start {inst_name} of {profile}")


@retry(RuntimeError, delay=5)
def wait_vm_state(profile, inst_name, state):
    """원하는 VM 상태가 될 때까지 재시도"""
    inst = ec2inst_by_name(profile, 'kafka')
    _state = inst.state['Name']
    if _state == state:
        print(f"[v] wait_vm_state {inst_name} {state}.. (now {_state})")
    else:
        print(f"[ ] wait_vm_state {inst_name} {state}.. (now {_state})")
        raise RuntimeError("VM state mismtach.")



def count_rows(db_type, cursor):
    tbl = 'person' if db_type == 'mysql' else '[test].[dbo].[person]'
    cursor.execute(f'''
    SELECT COUNT(*) cnt
    FROM {tbl}
    ''')
    res = cursor.fetchone()
    return res[0]


def count_table_row(profile):
    """테이블 행수 얻기."""
    _, cursor = db_concur(profile)
    return count_rows(profile, cursor)


@pytest.fixture
def xcdc(xprofile, xtable):
    """CDC 가능 처리.

    MSSQL 에서만 동작

    """
    if xprofile != 'mssql':
        return
    hash = binascii.hexlify(os.urandom(3)).decode('utf8')
    cap_inst = f'dbo_person_{hash}'
    print(f"xcdc : capture_instance {cap_inst}")
    sql = f'''
USE test;

EXEC sys.sp_cdc_enable_db

EXEC sys.sp_cdc_enable_table
    @source_schema = N'dbo',
    @source_name = N'person',
    @role_name = NULL,
    @capture_instance = {cap_inst},
    @supports_net_changes = 1

COMMIT;
    '''
    _, cursor = db_concur(xprofile)
    cursor.execute(sql)