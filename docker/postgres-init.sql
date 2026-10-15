-- Runs once, on the first start of an empty postgres volume.
CREATE DATABASE orbit_test OWNER orbit;
\c orbit
CREATE EXTENSION IF NOT EXISTS vector;
\c orbit_test
CREATE EXTENSION IF NOT EXISTS vector;
