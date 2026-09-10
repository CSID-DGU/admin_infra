-- operation_log (v1.0)
--
-- log-mysql 인스턴스의 operation_state_db에 적용한다.
--   kubectl -n ailab-infra exec -it log-mysql-0 -- \
--     mysql -u root -p operation_state_db < infra-sql/operation_log.sql
--
-- 이 파일은 config-server/operation_log.py의 INSERT 컬럼 목록과 1:1로 맞춰져 있다.
-- 컬럼을 추가하거나 이름을 바꾸면 두 곳을 함께 고쳐야 한다.
--
-- 저장 항목은 승인 1건을 단위로 한다. request_id에는 admin_be의 신청 PK가 그대로 들어가며,
-- config-server가 자체 생성하지 않는다. 이 값이 있어야 계정 생성·Pod 생성·회수가 하나의
-- 승인 아래로 묶여 단계별 소요시간과 수렴 시간을 산출할 수 있다.

CREATE TABLE IF NOT EXISTS operation_log (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id    VARCHAR(64) NOT NULL,   -- 승인 번호. admin_be가 보내는 신청 PK를 그대로 사용
  username      VARCHAR(64) NOT NULL,
  pod_name      VARCHAR(255),           -- 알기 전엔 NULL, Pod 이름이 정해지면 채움
  node_name     VARCHAR(64),            -- 마찬가지로 노드가 정해지면 채움
  resource_type VARCHAR(32),            -- account/kerberos/storage/pod/service/nodeport
  action        VARCHAR(64) NOT NULL,   -- operation_log.py의 Action Enum 값
  phase         VARCHAR(16) NOT NULL,   -- START/SUCCESS/FAIL/RETRY (UNKNOWN은 v2.0에서 추가)
  attempt       SMALLINT DEFAULT 1,
  duration_ms   INT,                    -- SUCCESS/FAIL 행에만 채움: 같은 (request_id, action, attempt)의 START로부터 걸린 시간
  error_code    VARCHAR(64),
  error_detail  TEXT,
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_req (request_id, created_at),
  INDEX idx_action_phase (action, phase)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;  -- 운영 log-mysql(SHOW CREATE TABLE)과 동일
