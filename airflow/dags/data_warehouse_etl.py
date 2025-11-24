# airflow/dags/data_warehouse_etl.py
"""
Data Warehouse ETL Pipeline
Extracts data from MongoDB and PostgreSQL sources based on actual schemas
Transforms and loads into the data warehouse
Runs every 6 hours
"""

from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
import os
import logging
from pymongo import MongoClient
import psycopg2
from psycopg2.extras import execute_batch
import json

# Default arguments for the DAG
default_args = {
    'owner': 'data-team',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=1),
}

# Create the DAG
dag = DAG(
    'data_warehouse_etl',
    default_args=default_args,
    description='ETL pipeline to load game data into warehouse',
    schedule_interval='0 */6 * * *',  # Every 6 hours
    catchup=False,
    tags=['etl', 'warehouse', 'game'],
)


def get_warehouse_connection():
    """Get warehouse database connection"""
    warehouse_uri = os.getenv('WAREHOUSE_URI')
    return psycopg2.connect(warehouse_uri)


def extract_users_from_mongo(**context):
    """Extract users from MongoDB UserService.users"""
    logging.info("Starting user extraction from MongoDB")

    mongo_uri = os.getenv('MONGO_PRIMARY_URI')
    client = MongoClient(f"{mongo_uri}/user_management?replicaSet=rs0")
    db = client['user_management']

    users = list(db.users.find({}))
    logging.info(f"Extracted {len(users)} users from MongoDB")

    # Load into staging
    conn = get_warehouse_connection()
    cur = conn.cursor()

    # Clear staging table
    cur.execute("TRUNCATE TABLE staging.users_staging")

    # Insert data
    insert_query = """
        INSERT INTO staging.users_staging 
        (user_id, username, email, platform, browser, os, country, timezone)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    data = [
        (
            str(user.get('_id', '')),
            user.get('username'),
            user.get('email'),
            user.get('device_info', {}).get('platform'),
            user.get('device_info', {}).get('browser'),
            user.get('device_info', {}).get('os'),
            user.get('location_info', {}).get('country'),
            user.get('location_info', {}).get('timezone')
        )
        for user in users
    ]

    execute_batch(cur, insert_query, data)
    conn.commit()
    cur.close()
    conn.close()
    client.close()

    logging.info(f"Loaded {len(data)} users into staging")
    return len(data)


def extract_characters_from_mongo(**context):
    """Extract characters from MongoDB CharacterService.characters"""
    logging.info("Starting character extraction from MongoDB")

    mongo_uri = os.getenv('MONGO_CHARACTER_URI')
    client = MongoClient(f"{mongo_uri}/characterDB?replicaSet=rs-character")
    db = client['characterDB']

    characters = list(db.characters.find({}))
    logging.info(f"Extracted {len(characters)} characters from MongoDB")

    # Load into staging
    conn = get_warehouse_connection()
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE staging.characters_staging")

    insert_query = """
        INSERT INTO staging.characters_staging 
        (character_id, character_business_id, user_id, role_id, level, gold, alive, inventory_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    data = [
        (
            str(char.get('_id', '')),
            char.get('characterId'),
            char.get('userId'),
            char.get('roleId'),
            char.get('level', 1),
            char.get('gold', 0),
            char.get('alive', True),
            len(char.get('inventory', []))
        )
        for char in characters
    ]

    execute_batch(cur, insert_query, data)
    conn.commit()
    cur.close()
    conn.close()
    client.close()

    logging.info(f"Loaded {len(data)} characters into staging")
    return len(data)


def extract_lobbies_from_mongo(**context):
    """Extract lobbies from MongoDB GameService.lobbies"""
    logging.info("Starting lobby extraction from MongoDB")

    mongo_uri = os.getenv('MONGO_PRIMARY_URI')
    client = MongoClient(f"{mongo_uri}/gameservice?replicaSet=rs0")
    db = client['gameservice']

    lobbies = list(db.lobbies.find({}))
    logging.info(f"Extracted {len(lobbies)} lobbies from MongoDB")

    # Load into staging
    conn = get_warehouse_connection()
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE staging.lobbies_staging")

    insert_query = """
        INSERT INTO staging.lobbies_staging 
        (lobby_id, max_players, current_state, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s)
    """

    data = [
        (
            str(lobby.get('_id', '')),
            lobby.get('max_players'),
            lobby.get('current_state'),
            lobby.get('created_at'),
            lobby.get('updated_at')
        )
        for lobby in lobbies
    ]

    execute_batch(cur, insert_query, data)
    conn.commit()
    cur.close()
    conn.close()
    client.close()

    logging.info(f"Loaded {len(data)} lobbies into staging")
    return len(data)


def extract_locations_from_mongo(**context):
    """Extract locations from MongoDB TownService.locations"""
    logging.info("Starting location extraction from MongoDB")

    mongo_uri = os.getenv('MONGO_TOWN_URI')
    client = MongoClient(mongo_uri)
    db = client['townDB']

    locations = list(db.locations.find({}))
    logging.info(f"Extracted {len(locations)} locations from MongoDB")

    # Load into staging
    conn = get_warehouse_connection()
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE staging.locations_staging")

    insert_query = """
        INSERT INTO staging.locations_staging 
        (location_id, location_name, lobby_id, occupants_json)
        VALUES (%s, %s, %s, %s)
    """

    data = [
        (
            location.get('locationId'),
            location.get('name'),
            str(location.get('lobbyId', '')),
            json.dumps(location.get('occupants', []))
        )
        for location in locations
    ]

    execute_batch(cur, insert_query, data)
    conn.commit()
    cur.close()
    conn.close()
    client.close()

    logging.info(f"Loaded {len(data)} locations into staging")
    return len(data)


def extract_tasks_from_postgres(**context):
    """Extract tasks from PostgreSQL TaskService.tasks"""
    logging.info("Starting task extraction from PostgreSQL")

    task_uri = os.getenv('POSTGRES_TASK_URI')
    source_conn = psycopg2.connect(task_uri)
    source_cur = source_conn.cursor()

    # Extract tasks
    source_cur.execute("""
        SELECT task_id, name, description, max_assignees_per_location, 
               payment_amount, created_at, updated_at
        FROM tasks
        WHERE updated_at > NOW() - INTERVAL '7 days'
        OR created_at > NOW() - INTERVAL '7 days'
    """)

    tasks = source_cur.fetchall()
    logging.info(f"Extracted {len(tasks)} tasks from PostgreSQL")

    # Load into staging
    warehouse_conn = get_warehouse_connection()
    warehouse_cur = warehouse_conn.cursor()

    warehouse_cur.execute("TRUNCATE TABLE staging.tasks_staging")

    insert_query = """
        INSERT INTO staging.tasks_staging 
        (task_id, task_name, task_description, max_assignees_per_location, 
         payment_amount, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    execute_batch(warehouse_cur, insert_query, tasks)
    warehouse_conn.commit()

    warehouse_cur.close()
    warehouse_conn.close()
    source_cur.close()
    source_conn.close()

    logging.info(f"Loaded {len(tasks)} tasks into staging")
    return len(tasks)


def extract_task_assignments_from_postgres(**context):
    """Extract task assignments from PostgreSQL TaskService.tasks_history"""
    logging.info("Starting task assignments extraction from PostgreSQL")

    task_uri = os.getenv('POSTGRES_TASK_URI')
    source_conn = psycopg2.connect(task_uri)
    source_cur = source_conn.cursor()

    # Extract task assignments from last 7 days
    source_cur.execute("""
        SELECT task_character_id, character_id, task_id, location_id, 
               lobby_id, assigned_at, completed_at
        FROM tasks_history
        WHERE assigned_at > NOW() - INTERVAL '7 days'
    """)

    assignments = source_cur.fetchall()
    logging.info(f"Extracted {len(assignments)} task assignments from PostgreSQL")

    # Load into staging
    warehouse_conn = get_warehouse_connection()
    warehouse_cur = warehouse_conn.cursor()

    warehouse_cur.execute("TRUNCATE TABLE staging.task_assignments_staging")

    insert_query = """
        INSERT INTO staging.task_assignments_staging 
        (task_character_id, character_id, task_id, location_id, 
         lobby_id, assigned_at, completed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    execute_batch(warehouse_cur, insert_query, assignments)
    warehouse_conn.commit()

    warehouse_cur.close()
    warehouse_conn.close()
    source_cur.close()
    source_conn.close()

    logging.info(f"Loaded {len(assignments)} task assignments into staging")
    return len(assignments)


def extract_votes_from_postgres(**context):
    """Extract votes from PostgreSQL VotingService.votes"""
    logging.info("Starting votes extraction from PostgreSQL")

    voting_uri = os.getenv('POSTGRES_VOTING_URI')
    source_conn = psycopg2.connect(voting_uri)
    source_cur = source_conn.cursor()

    # Extract votes from last 7 days
    source_cur.execute("""
        SELECT vote_id, lobby_id, voter_character_id, voted_character_id,
               weight, voted_at, created_at
        FROM votes
        WHERE voted_at > NOW() - INTERVAL '7 days'
    """)

    votes = source_cur.fetchall()
    logging.info(f"Extracted {len(votes)} votes from PostgreSQL")

    # Load into staging
    warehouse_conn = get_warehouse_connection()
    warehouse_cur = warehouse_conn.cursor()

    warehouse_cur.execute("TRUNCATE TABLE staging.votes_staging")

    insert_query = """
        INSERT INTO staging.votes_staging 
        (vote_id, lobby_id, voter_character_id, voted_character_id,
         weight, voted_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    execute_batch(warehouse_cur, insert_query, votes)
    warehouse_conn.commit()

    warehouse_cur.close()
    warehouse_conn.close()
    source_cur.close()
    source_conn.close()

    logging.info(f"Loaded {len(votes)} votes into staging")
    return len(votes)


def extract_roles_from_postgres(**context):
    """Extract roles from PostgreSQL VotingService.role_vote_power"""
    logging.info("Starting roles extraction from PostgreSQL")

    voting_uri = os.getenv('POSTGRES_VOTING_URI')
    source_conn = psycopg2.connect(voting_uri)
    source_cur = source_conn.cursor()

    # Extract all roles
    source_cur.execute("""
        SELECT role_id, vote_power, date_created, date_updated
        FROM role_vote_power
    """)

    roles = source_cur.fetchall()
    logging.info(f"Extracted {len(roles)} roles from PostgreSQL")

    # Load into staging
    warehouse_conn = get_warehouse_connection()
    warehouse_cur = warehouse_conn.cursor()

    warehouse_cur.execute("TRUNCATE TABLE staging.roles_staging")

    insert_query = """
        INSERT INTO staging.roles_staging 
        (role_id, vote_power, date_created, date_updated)
        VALUES (%s, %s, %s, %s)
    """

    execute_batch(warehouse_cur, insert_query, roles)
    warehouse_conn.commit()

    warehouse_cur.close()
    warehouse_conn.close()
    source_cur.close()
    source_conn.close()

    logging.info(f"Loaded {len(roles)} roles into staging")
    return len(roles)


def extract_rumors_from_postgres(**context):
    """Extract rumors from PostgreSQL RumorsService.rumors"""
    logging.info("Starting rumors extraction from PostgreSQL")

    rumors_uri = os.getenv('POSTGRES_RUMORS_URI')
    source_conn = psycopg2.connect(rumors_uri)
    source_cur = source_conn.cursor()

    # Extract rumors from last 7 days
    source_cur.execute("""
        SELECT rumor_id, origin, recipient_character_id, rumor_text, timestamp
        FROM rumors
        WHERE timestamp > NOW() - INTERVAL '7 days'
    """)

    rumors = source_cur.fetchall()
    logging.info(f"Extracted {len(rumors)} rumors from PostgreSQL")

    # Load into staging
    warehouse_conn = get_warehouse_connection()
    warehouse_cur = warehouse_conn.cursor()

    warehouse_cur.execute("TRUNCATE TABLE staging.rumors_staging")

    insert_query = """
        INSERT INTO staging.rumors_staging 
        (rumor_id, origin, recipient_character_id, rumor_text, timestamp)
        VALUES (%s, %s, %s, %s, %s)
    """

    execute_batch(warehouse_cur, insert_query, rumors)
    warehouse_conn.commit()

    warehouse_cur.close()
    warehouse_conn.close()
    source_cur.close()
    source_conn.close()

    logging.info(f"Loaded {len(rumors)} rumors into staging")
    return len(rumors)


def load_dimensions(**context):
    """Load dimension tables from staging using SCD Type 2 where appropriate"""
    logging.info("Loading dimension tables")

    conn = get_warehouse_connection()
    cur = conn.cursor()

    # Load users dimension with SCD Type 2
    cur.execute("""
        -- Close out changed records
        UPDATE dim.users u
        SET valid_to = CURRENT_TIMESTAMP,
            is_current = FALSE
        FROM staging.users_staging s
        WHERE u.user_id = s.user_id
        AND u.is_current = TRUE
        AND (
            COALESCE(u.username, '') != COALESCE(s.username, '') OR
            COALESCE(u.email, '') != COALESCE(s.email, '') OR
            COALESCE(u.platform, '') != COALESCE(s.platform, '') OR
            COALESCE(u.country, '') != COALESCE(s.country, '')
        );

        -- Insert new and changed records
        INSERT INTO dim.users 
        (user_id, username, email, platform, browser, os, country, timezone, valid_from, is_current)
        SELECT 
            s.user_id, s.username, s.email, s.platform, s.browser, s.os, s.country, s.timezone,
            CURRENT_TIMESTAMP, TRUE
        FROM staging.users_staging s
        LEFT JOIN dim.users u ON s.user_id = u.user_id AND u.is_current = TRUE
        WHERE u.user_key IS NULL
        OR COALESCE(u.username, '') != COALESCE(s.username, '')
        OR COALESCE(u.email, '') != COALESCE(s.email, '')
        OR COALESCE(u.platform, '') != COALESCE(s.platform, '')
        OR COALESCE(u.country, '') != COALESCE(s.country, '');
    """)

    # Load characters dimension with SCD Type 2
    cur.execute("""
        UPDATE dim.characters c
        SET valid_to = CURRENT_TIMESTAMP,
            is_current = FALSE
        FROM staging.characters_staging s
        WHERE c.character_id = s.character_id
        AND c.is_current = TRUE
        AND (
            c.level != s.level OR
            c.gold != s.gold OR
            c.alive != s.alive OR
            c.role_id != s.role_id
        );

        INSERT INTO dim.characters 
        (character_id, character_business_id, user_id, role_id, level, gold, 
         alive, inventory_count, valid_from, is_current)
        SELECT 
            s.character_id, s.character_business_id, s.user_id, s.role_id, 
            s.level, s.gold, s.alive, s.inventory_count,
            CURRENT_TIMESTAMP, TRUE
        FROM staging.characters_staging s
        LEFT JOIN dim.characters c ON s.character_id = c.character_id AND c.is_current = TRUE
        WHERE c.character_key IS NULL
        OR c.level != s.level
        OR c.gold != s.gold
        OR c.alive != s.alive
        OR c.role_id != s.role_id;
    """)

    # Load lobbies dimension
    cur.execute("""
        INSERT INTO dim.lobbies (lobby_id, max_players, current_state, created_at, updated_at, is_active)
        SELECT lobby_id, max_players, current_state, created_at, updated_at, TRUE
        FROM staging.lobbies_staging
        ON CONFLICT (lobby_id) DO UPDATE SET
            current_state = EXCLUDED.current_state,
            updated_at = EXCLUDED.updated_at;
    """)

    # Load locations dimension
    cur.execute("""
        INSERT INTO dim.locations (location_id, location_name, lobby_id, is_active)
        SELECT location_id, location_name, lobby_id, TRUE
        FROM staging.locations_staging
        ON CONFLICT (location_id) DO UPDATE SET
            location_name = EXCLUDED.location_name,
            lobby_id = EXCLUDED.lobby_id;
    """)

    # Load tasks dimension
    cur.execute("""
        INSERT INTO dim.tasks 
        (task_id, task_name, task_description, max_assignees_per_location, 
         payment_amount, created_at, updated_at, is_active)
        SELECT 
            task_id, task_name, task_description, max_assignees_per_location,
            payment_amount, created_at, updated_at, TRUE
        FROM staging.tasks_staging
        ON CONFLICT (task_id) DO UPDATE SET
            task_name = EXCLUDED.task_name,
            task_description = EXCLUDED.task_description,
            max_assignees_per_location = EXCLUDED.max_assignees_per_location,
            payment_amount = EXCLUDED.payment_amount,
            updated_at = EXCLUDED.updated_at;
    """)

    # Load roles dimension
    cur.execute("""
        INSERT INTO dim.roles (role_id, vote_power, created_at, updated_at)
        SELECT role_id, vote_power, date_created, date_updated
        FROM staging.roles_staging
        ON CONFLICT (role_id) DO UPDATE SET
            vote_power = EXCLUDED.vote_power,
            updated_at = EXCLUDED.updated_at;
    """)

    # Load rumors dimension
    cur.execute("""
        INSERT INTO dim.rumors (rumor_id, origin, rumor_text, created_at)
        SELECT rumor_id, origin, rumor_text, timestamp
        FROM staging.rumors_staging
        ON CONFLICT (rumor_id) DO NOTHING;
    """)

    conn.commit()
    cur.close()
    conn.close()

    logging.info("Dimension tables loaded successfully")


def load_fact_tables(**context):
    """Load fact tables from staging"""
    logging.info("Loading fact tables")

    conn = get_warehouse_connection()
    cur = conn.cursor()

    # Load task assignments fact
    cur.execute("""
        INSERT INTO fact.task_assignments 
        (date_key, task_key, character_key, location_key, lobby_key,
         assigned_at, completed_at, time_to_complete_seconds, payment_received, was_completed)
        SELECT 
            TO_CHAR(s.assigned_at, 'YYYYMMDD')::INTEGER,
            t.task_key,
            c.character_key,
            l.location_key,
            lb.lobby_key,
            s.assigned_at,
            s.completed_at,
            CASE 
                WHEN s.completed_at IS NOT NULL 
                THEN EXTRACT(EPOCH FROM (s.completed_at - s.assigned_at))::INTEGER
                ELSE NULL
            END,
            CASE WHEN s.completed_at IS NOT NULL THEN t.payment_amount ELSE 0 END,
            s.completed_at IS NOT NULL
        FROM staging.task_assignments_staging s
        JOIN dim.tasks t ON s.task_id = t.task_id
        JOIN dim.characters c ON s.character_id = c.character_business_id AND c.is_current = TRUE
        LEFT JOIN dim.locations l ON s.location_id = l.location_id
        LEFT JOIN dim.lobbies lb ON s.lobby_id = lb.lobby_id
        WHERE NOT EXISTS (
            SELECT 1 FROM fact.task_assignments fa 
            WHERE fa.character_key = c.character_key 
            AND fa.task_key = t.task_key 
            AND fa.assigned_at = s.assigned_at
        );
    """)

    # Load votes fact
    cur.execute("""
        INSERT INTO fact.votes 
        (date_key, lobby_key, voter_character_key, voted_character_key, vote_weight, voted_at)
        SELECT 
            TO_CHAR(s.voted_at, 'YYYYMMDD')::INTEGER,
            lb.lobby_key,
            c1.character_key,
            c2.character_key,
            s.weight,
            s.voted_at
        FROM staging.votes_staging s
        JOIN dim.lobbies lb ON s.lobby_id = lb.lobby_id
        JOIN dim.characters c1 ON s.voter_character_id = c1.character_business_id AND c1.is_current = TRUE
        JOIN dim.characters c2 ON s.voted_character_id = c2.character_business_id AND c2.is_current = TRUE
        WHERE NOT EXISTS (
            SELECT 1 FROM fact.votes fv 
            WHERE fv.voter_character_key = c1.character_key 
            AND fv.voted_character_key = c2.character_key 
            AND fv.voted_at = s.voted_at
        );
    """)

    # Load rumor messages fact
    cur.execute("""
        INSERT INTO fact.rumor_messages 
        (date_key, rumor_key, recipient_character_key, message_timestamp, origin)
        SELECT 
            TO_CHAR(s.timestamp, 'YYYYMMDD')::INTEGER,
            r.rumor_key,
            c.character_key,
            s.timestamp,
            s.origin
        FROM staging.rumors_staging s
        JOIN dim.rumors r ON s.rumor_id = r.rumor_id
        LEFT JOIN dim.characters c ON s.recipient_character_id = c.character_business_id AND c.is_current = TRUE
        WHERE NOT EXISTS (
            SELECT 1 FROM fact.rumor_messages frm 
            WHERE frm.rumor_key = r.rumor_key 
            AND COALESCE(frm.recipient_character_key, -1) = COALESCE(c.character_key, -1)
            AND frm.message_timestamp = s.timestamp
        );
    """)

    conn.commit()
    cur.close()
    conn.close()

    logging.info("Fact tables loaded successfully")


def data_quality_checks(**context):
    """Perform data quality checks"""
    logging.info("Running data quality checks")

    conn = get_warehouse_connection()
    cur = conn.cursor()

    checks = []

    # Check for null user_ids in users dimension
    cur.execute("SELECT COUNT(*) FROM dim.users WHERE user_id IS NULL")
    null_users = cur.fetchone()[0]
    checks.append(('null_user_ids', null_users, 0))

    # Check for duplicate current records in users
    cur.execute("""
        SELECT user_id, COUNT(*) 
        FROM dim.users 
        WHERE is_current = TRUE 
        GROUP BY user_id 
        HAVING COUNT(*) > 1
    """)
    duplicate_users = len(cur.fetchall())
    checks.append(('duplicate_current_users', duplicate_users, 0))

    # Check for orphan task assignments
    cur.execute("""
        SELECT COUNT(*) 
        FROM fact.task_assignments ta
        LEFT JOIN dim.characters c ON ta.character_key = c.character_key
        WHERE c.character_key IS NULL
    """)
    orphan_assignments = cur.fetchone()[0]
    checks.append(('orphan_task_assignments', orphan_assignments, 0))

    # Check for votes without valid characters
    cur.execute("""
        SELECT COUNT(*) 
        FROM fact.votes v
        LEFT JOIN dim.characters c ON v.voter_character_key = c.character_key
        WHERE c.character_key IS NULL
    """)
    orphan_votes = cur.fetchone()[0]
    checks.append(('orphan_votes', orphan_votes, 0))

    cur.close()
    conn.close()

    # Log results
    failed_checks = []
    for check_name, actual, expected in checks:
        if actual != expected:
            failed_checks.append(f"{check_name}: expected {expected}, got {actual}")
            logging.warning(f"Data quality check failed: {check_name}")
        else:
            logging.info(f"Data quality check passed: {check_name}")

    if failed_checks:
        logging.error(f"Data quality checks failed: {', '.join(failed_checks)}")
    else:
        logging.info("All data quality checks passed")

    return len(failed_checks) == 0


# Define extract tasks for MongoDB sources
extract_users_task = PythonOperator(
    task_id='extract_users_from_mongo',
    python_callable=extract_users_from_mongo,
    dag=dag,
)

extract_characters_task = PythonOperator(
    task_id='extract_characters_from_mongo',
    python_callable=extract_characters_from_mongo,
    dag=dag,
)

extract_lobbies_task = PythonOperator(
    task_id='extract_lobbies_from_mongo',
    python_callable=extract_lobbies_from_mongo,
    dag=dag,
)

extract_locations_task = PythonOperator(
    task_id='extract_locations_from_mongo',
    python_callable=extract_locations_from_mongo,
    dag=dag,
)

# Define extract tasks for PostgreSQL sources
extract_tasks_task = PythonOperator(
    task_id='extract_tasks_from_postgres',
    python_callable=extract_tasks_from_postgres,
    dag=dag,
)

extract_task_assignments_task = PythonOperator(
    task_id='extract_task_assignments_from_postgres',
    python_callable=extract_task_assignments_from_postgres,
    dag=dag,
)

extract_votes_task = PythonOperator(
    task_id='extract_votes_from_postgres',
    python_callable=extract_votes_from_postgres,
    dag=dag,
)

extract_roles_task = PythonOperator(
    task_id='extract_roles_from_postgres',
    python_callable=extract_roles_from_postgres,
    dag=dag,
)

extract_rumors_task = PythonOperator(
    task_id='extract_rumors_from_postgres',
    python_callable=extract_rumors_from_postgres,
    dag=dag,
)

# Define load tasks
load_dimensions_task = PythonOperator(
    task_id='load_dimensions',
    python_callable=load_dimensions,
    dag=dag,
)

load_facts_task = PythonOperator(
    task_id='load_fact_tables',
    python_callable=load_fact_tables,
    dag=dag,
)

quality_checks_task = PythonOperator(
    task_id='data_quality_checks',
    python_callable=data_quality_checks,
    dag=dag,
)

# Define task dependencies
# All extracts run in parallel, then load dimensions, then load facts, then quality checks
[
    extract_users_task,
    extract_characters_task,
    extract_lobbies_task,
    extract_locations_task,
    extract_tasks_task,
    extract_task_assignments_task,
    extract_votes_task,
    extract_roles_task,
    extract_rumors_task
] >> load_dimensions_task >> load_facts_task >> quality_checks_task