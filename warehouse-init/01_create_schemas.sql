-- warehouse-init/01_create_schemas.sql
-- Data Warehouse Schema aligned with actual microservices schemas

-- Create schemas for dimensional modeling
CREATE SCHEMA IF NOT EXISTS dim;
CREATE SCHEMA IF NOT EXISTS fact;
CREATE SCHEMA IF NOT EXISTS staging;

-- =============================
-- DIMENSION TABLES
-- =============================



-- User dimension (from UserService.users MongoDB)
CREATE TABLE IF NOT EXISTS dim.users (
    user_key SERIAL PRIMARY KEY,
    user_id VARCHAR(50) UNIQUE NOT NULL, -- MongoDB _id
    username VARCHAR(100),
    email VARCHAR(255),
    platform VARCHAR(50), -- device_info.platform
    browser VARCHAR(50),  -- device_info.browser
    os VARCHAR(100),      -- device_info.os
    country VARCHAR(100), -- location_info.country
    timezone VARCHAR(50), -- location_info.timezone
    created_at TIMESTAMP,
    valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMP,
    is_current BOOLEAN DEFAULT TRUE
);

-- Character dimension (from CharacterService.characters MongoDB)
CREATE TABLE IF NOT EXISTS dim.characters (
    character_key SERIAL PRIMARY KEY,
    character_id VARCHAR(50) UNIQUE NOT NULL, -- MongoDB _id
    character_business_id VARCHAR(50), -- characterId field
    user_id VARCHAR(50),
    role_id INTEGER,
    level INTEGER,
    gold INTEGER,
    alive BOOLEAN,
    inventory_count INTEGER, -- Count of items in inventory array
    created_at TIMESTAMP,
    valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMP,
    is_current BOOLEAN DEFAULT TRUE
);

-- Role dimension (from VotingService.role_vote_power PostgreSQL)
CREATE TABLE IF NOT EXISTS dim.roles (
    role_key SERIAL PRIMARY KEY,
    role_id BIGINT UNIQUE NOT NULL,
    vote_power INTEGER,
    role_name VARCHAR(100), -- Can be populated from game config
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Lobby dimension (from GameService.lobbies MongoDB)
CREATE TABLE IF NOT EXISTS dim.lobbies (
    lobby_key SERIAL PRIMARY KEY,
    lobby_id VARCHAR(50) UNIQUE NOT NULL, -- MongoDB _id
    max_players INTEGER,
    current_state VARCHAR(50), -- waiting, in_progress, completed
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE
);

-- Location dimension (from TownService.locations MongoDB)
CREATE TABLE IF NOT EXISTS dim.locations (
    location_key SERIAL PRIMARY KEY,
    location_id BIGINT UNIQUE NOT NULL,
    location_name VARCHAR(255),
    lobby_id VARCHAR(50),
    created_at TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE
);

-- Task dimension (from TaskService.tasks PostgreSQL)
CREATE TABLE IF NOT EXISTS dim.tasks (
    task_key SERIAL PRIMARY KEY,
    task_id BIGINT UNIQUE NOT NULL,
    task_name VARCHAR(255),
    task_description TEXT,
    max_assignees_per_location INTEGER,
    payment_amount DECIMAL(10,2),
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE
);

-- Rumor dimension (from RumorsService.rumors PostgreSQL)
CREATE TABLE IF NOT EXISTS dim.rumors (
    rumor_key SERIAL PRIMARY KEY,
    rumor_id BIGINT UNIQUE NOT NULL,
    origin VARCHAR(255),
    rumor_text TEXT,
    created_at TIMESTAMP
);

-- =============================
-- FACT TABLES
-- =============================

-- Lobby sessions fact (game rounds)
CREATE TABLE IF NOT EXISTS fact.lobby_sessions (
    session_key BIGSERIAL PRIMARY KEY,
    lobby_key INTEGER,
    session_start TIMESTAMP NOT NULL,
    session_end TIMESTAMP,
    session_duration_seconds INTEGER,
    max_players INTEGER,
    final_state VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Character sessions fact (character activity in lobbies)
CREATE TABLE IF NOT EXISTS fact.character_sessions (
    char_session_key BIGSERIAL PRIMARY KEY,
    date_key INTEGER,
    lobby_key INTEGER,
    character_key INTEGER,
    user_key INTEGER,
    role_key INTEGER,
    location_key INTEGER,
    session_start TIMESTAMP,
    session_end TIMESTAMP,
    starting_gold INTEGER,
    ending_gold INTEGER,
    gold_change INTEGER,
    survived BOOLEAN, -- Final alive status
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Task assignments fact (from TaskService.tasks_history PostgreSQL)
CREATE TABLE IF NOT EXISTS fact.task_assignments (
    assignment_key BIGSERIAL PRIMARY KEY,
    date_key INTEGER,
    task_key INTEGER,
    character_key INTEGER,
    location_key INTEGER,
    lobby_key INTEGER,
    assigned_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    time_to_complete_seconds INTEGER,
    payment_received DECIMAL(10,2),
    was_completed BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Voting fact (from VotingService.votes PostgreSQL)
CREATE TABLE IF NOT EXISTS fact.votes (
    vote_key BIGSERIAL PRIMARY KEY,
    date_key INTEGER,
    lobby_key INTEGER,
    voter_character_key INTEGER,
    voted_character_key INTEGER,
    vote_weight INTEGER,
    voted_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Rumor interactions fact (from RumorsService.rumors PostgreSQL)
CREATE TABLE IF NOT EXISTS fact.rumor_messages (
    rumor_message_key BIGSERIAL PRIMARY KEY,
    date_key INTEGER,
    rumor_key INTEGER,
    recipient_character_key INTEGER,
    message_timestamp TIMESTAMP NOT NULL,
    origin VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Location occupancy fact (snapshot of location occupants)
CREATE TABLE IF NOT EXISTS fact.location_occupancy (
    occupancy_key BIGSERIAL PRIMARY KEY,
    date_key INTEGER,
    location_key INTEGER,
    lobby_key INTEGER,
    character_key INTEGER,
    entered_at TIMESTAMP,
    left_at TIMESTAMP,
    duration_seconds INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================
-- STAGING TABLES (for ETL)
-- =============================

-- Users staging
CREATE TABLE IF NOT EXISTS staging.users_staging (
    user_id VARCHAR(50),
    username VARCHAR(100),
    email VARCHAR(255),
    platform VARCHAR(50),
    browser VARCHAR(50),
    os VARCHAR(100),
    country VARCHAR(100),
    timezone VARCHAR(50),
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Characters staging
CREATE TABLE IF NOT EXISTS staging.characters_staging (
    character_id VARCHAR(50),
    character_business_id VARCHAR(50),
    user_id VARCHAR(50),
    role_id INTEGER,
    level INTEGER,
    gold INTEGER,
    alive BOOLEAN,
    inventory_count INTEGER,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Lobbies staging
CREATE TABLE IF NOT EXISTS staging.lobbies_staging (
    lobby_id VARCHAR(50),
    max_players INTEGER,
    current_state VARCHAR(50),
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Locations staging
CREATE TABLE IF NOT EXISTS staging.locations_staging (
    location_id BIGINT,
    location_name VARCHAR(255),
    lobby_id VARCHAR(50),
    occupants_json TEXT, -- JSON array of occupants
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tasks staging
CREATE TABLE IF NOT EXISTS staging.tasks_staging (
    task_id BIGINT,
    task_name VARCHAR(255),
    task_description TEXT,
    max_assignees_per_location INTEGER,
    payment_amount DECIMAL(10,2),
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Task assignments staging
CREATE TABLE IF NOT EXISTS staging.task_assignments_staging (
    task_character_id BIGINT,
    character_id VARCHAR(255),
    task_id BIGINT,
    location_id BIGINT,
    lobby_id VARCHAR(255),
    assigned_at TIMESTAMP,
    completed_at TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Votes staging
CREATE TABLE IF NOT EXISTS staging.votes_staging (
    vote_id BIGINT,
    lobby_id VARCHAR(255),
    voter_character_id VARCHAR(255),
    voted_character_id VARCHAR(255),
    weight INTEGER,
    voted_at TIMESTAMP,
    created_at TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Roles staging
CREATE TABLE IF NOT EXISTS staging.roles_staging (
    role_id BIGINT,
    vote_power INTEGER,
    date_created TIMESTAMP,
    date_updated TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Rumors staging
CREATE TABLE IF NOT EXISTS staging.rumors_staging (
    rumor_id BIGINT,
    origin VARCHAR(255),
    recipient_character_id VARCHAR(255),
    rumor_text TEXT,
    timestamp TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================
-- INDEXES for Performance
-- =============================

-- Dimension indexes
CREATE INDEX IF NOT EXISTS idx_users_user_id ON dim.users(user_id);
CREATE INDEX IF NOT EXISTS idx_users_current ON dim.users(is_current);
CREATE INDEX IF NOT EXISTS idx_characters_char_id ON dim.characters(character_id);
CREATE INDEX IF NOT EXISTS idx_characters_user_id ON dim.characters(user_id);
CREATE INDEX IF NOT EXISTS idx_characters_current ON dim.characters(is_current);
CREATE INDEX IF NOT EXISTS idx_lobbies_state ON dim.lobbies(current_state);
CREATE INDEX IF NOT EXISTS idx_locations_lobby ON dim.locations(lobby_id);

-- Fact indexes
CREATE INDEX IF NOT EXISTS idx_char_sessions_date ON fact.character_sessions(date_key);
CREATE INDEX IF NOT EXISTS idx_char_sessions_lobby ON fact.character_sessions(lobby_key);
CREATE INDEX IF NOT EXISTS idx_char_sessions_char ON fact.character_sessions(character_key);
CREATE INDEX IF NOT EXISTS idx_task_assignments_date ON fact.task_assignments(date_key);
CREATE INDEX IF NOT EXISTS idx_task_assignments_char ON fact.task_assignments(character_key);
CREATE INDEX IF NOT EXISTS idx_task_assignments_lobby ON fact.task_assignments(lobby_key);
CREATE INDEX IF NOT EXISTS idx_votes_date ON fact.votes(date_key);
CREATE INDEX IF NOT EXISTS idx_votes_lobby ON fact.votes(lobby_key);
CREATE INDEX IF NOT EXISTS idx_rumor_messages_date ON fact.rumor_messages(date_key);

-- Create metabase database
CREATE DATABASE metabase;