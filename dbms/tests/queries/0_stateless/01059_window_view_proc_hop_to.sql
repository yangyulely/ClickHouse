SET allow_experimental_window_view = 1;

DROP TABLE IF EXISTS mt;
DROP TABLE IF EXISTS dst;
DROP TABLE IF EXISTS wv;

CREATE TABLE dst(count UInt64) Engine=MergeTree ORDER BY tuple();
CREATE TABLE mt(a Int32, timestamp DateTime) ENGINE=MergeTree ORDER BY tuple();
CREATE WINDOW VIEW wv TO dst AS SELECT count(a) AS count FROM mt GROUP BY HOP(timestamp, INTERVAL '1' SECOND, INTERVAL '1' SECOND) AS wid;

INSERT INTO mt VALUES (1, now());
SELECT sleep(2);
SELECT count from dst;

DROP TABLE wv;
DROP TABLE mt;
DROP TABLE dst;
