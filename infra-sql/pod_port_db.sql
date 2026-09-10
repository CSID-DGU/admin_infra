-- pod_port_db (config-server 운영 DB, infra-mysql 인스턴스)
--
--   kubectl -n <namespace> exec -i infra-mysql-0 -- \
--     mysql -u root -p pod_port_db < infra-sql/pod_port_db.sql
--
-- 운영 infra-mysql에서 스키마만 추출(mysqldump --no-data)한 것에서 AUTO_INCREMENT 현재값만 뺐다.
-- 테이블 정의가 레포에 없고 코드가 자동으로 만들지도 않아, 새 인스턴스를 띄우면 config-server가
-- 동작하지 않던 문제를 막기 위해 둔다. 인덱스는 운영과 똑같이 유지한다(node_port_2 중복 포함).
--
-- server_nodes(GPU 노드 목록)는 현재 config-server와 admin_be 코드 어디에서도 읽지 않는다.
-- 운영과 스키마를 맞추기 위해 정의만 둔다.

-- farm 노드 keytab 정리가 실패했을 때 재조정 크론잡(reconcile_krb5.py)이 다시 시도할 예약
CREATE TABLE IF NOT EXISTS `krb5_cleanup_pending` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL,
  `node_name` varchar(64) NOT NULL,
  `failed_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_username_node` (`username`,`node_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 사용자 Pod에 배정한 NodePort
CREATE TABLE IF NOT EXISTS `nodeport_allocations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(255) NOT NULL,
  `pod_name` varchar(255) NOT NULL,
  `node_name` varchar(255) NOT NULL,
  `internal_port` int NOT NULL,
  `node_port` int NOT NULL,
  `purpose` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `node_port` (`node_port`),
  KEY `node_port_2` (`node_port`),
  KEY `idx_pod_name` (`pod_name`),
  KEY `idx_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- GPU 노드 목록
CREATE TABLE IF NOT EXISTS `server_nodes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `node_id` varchar(20) NOT NULL,
  `cluster` varchar(10) NOT NULL COMMENT 'FARM or LAB',
  `gpu_model` varchar(100) NOT NULL,
  `gpu_count` int NOT NULL,
  `gpu_memory_mib` int NOT NULL,
  `status` enum('available','in_use','unavailable') NOT NULL DEFAULT 'available',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `node_id` (`node_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
