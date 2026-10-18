use arma_executor::config::ExecutorConfig;
use arma_executor::executor::ArmaExecutor;
use arma_executor::protocol::{
    ActionResult, CapabilitiesResult, ListResult, ShutdownResult, StatusResult,
};
use tempfile::tempdir;

fn create_simulated_executor() -> (ArmaExecutor, tempfile::TempDir) {
    let dir = tempdir().unwrap();
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "{dir}",
            "sysfs_root": "/sys/kernel/sched_ext",
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"], "kernel_ops": "bpfland"}},
                {{"id": "scx_rusty", "command": ["/usr/bin/scx_rusty"], "kernel_ops": "rusty"}},
                {{"id": "scx_failing", "command": ["simulate_fail"], "kernel_ops": "failing"}}
            ]
        }}"#,
        dir = dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    (ArmaExecutor::new(config), dir)
}

#[test]
fn test_capabilities() {
    let (mut executor, _dir) = create_simulated_executor();
    let raw = r#"{"schema_version":1,"request_id":"req-cap-1","operation":"capabilities","timeout_ms":5000,"params":{}}"#;
    let resp = executor.process_raw_request(raw);

    assert!(resp.ok);
    assert_eq!(resp.request_id, "req-cap-1");
    let caps: CapabilitiesResult = serde_json::from_value(resp.result.unwrap()).unwrap();
    assert_eq!(caps.backend, "simulated");
    assert!(caps.supported_operations.contains(&"apply".to_string()));
    assert!(caps.supported_operations.contains(&"rollback".to_string()));
    assert!(caps.supported_policies.contains(&"scx_bpfland".to_string()));
}

#[test]
fn test_list() {
    let (mut executor, _dir) = create_simulated_executor();
    let raw = r#"{"schema_version":1,"request_id":"req-list-1","operation":"list","timeout_ms":5000,"params":{}}"#;
    let resp = executor.process_raw_request(raw);

    assert!(resp.ok);
    let list_res: ListResult = serde_json::from_value(resp.result.unwrap()).unwrap();
    assert_eq!(list_res.policies.len(), 4);
    assert_eq!(list_res.policies[0].id, "default");
    assert_eq!(list_res.policies[1].id, "scx_bpfland");
}

#[test]
fn test_initial_status() {
    let (mut executor, _dir) = create_simulated_executor();
    let raw = r#"{"schema_version":1,"request_id":"req-status-1","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp = executor.process_raw_request(raw);

    assert!(resp.ok);
    let status: StatusResult = serde_json::from_value(resp.result.unwrap()).unwrap();
    assert_eq!(status.policy_id, "default");
    assert_eq!(status.kernel_state, "disabled");
    assert_eq!(status.kernel_ops, None);
    assert_eq!(status.managed_pid, None);
    assert!(status.healthy);
    assert_eq!(status.backend, "simulated");
}

#[test]
fn test_apply_and_held_and_dry_run() {
    let (mut executor, _dir) = create_simulated_executor();

    // 1. Dry run apply
    let raw_dry = r#"{"schema_version":1,"request_id":"req-dry-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999,"dry_run":true}}"#;
    let resp_dry = executor.process_raw_request(raw_dry);
    assert!(resp_dry.ok);
    let act_dry: ActionResult = serde_json::from_value(resp_dry.result.unwrap()).unwrap();
    assert_eq!(act_dry.status, "dry_run");
    assert_eq!(act_dry.planned_policy, "scx_bpfland");
    assert_eq!(act_dry.actual_policy, "default");
    assert!(act_dry.verified);
    assert_eq!(executor.current_policy, "default");

    // 2. Real apply
    let raw_apply = r#"{"schema_version":1,"request_id":"req-app-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999,"dry_run":false}}"#;
    let resp_apply = executor.process_raw_request(raw_apply);
    assert!(resp_apply.ok);
    let act_apply: ActionResult = serde_json::from_value(resp_apply.result.unwrap()).unwrap();
    assert_eq!(act_apply.status, "applied");
    assert_eq!(act_apply.planned_policy, "scx_bpfland");
    assert_eq!(act_apply.previous_policy, "default");
    assert_eq!(act_apply.actual_policy, "scx_bpfland");
    assert!(act_apply.verified);
    assert!(act_apply.managed_pid.is_some());
    assert_eq!(act_apply.backend, "simulated");

    // 3. Query status after apply
    let raw_status = r#"{"schema_version":1,"request_id":"req-status-2","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp_status = executor.process_raw_request(raw_status);
    let st: StatusResult = serde_json::from_value(resp_status.result.unwrap()).unwrap();
    assert_eq!(st.policy_id, "scx_bpfland");
    assert_eq!(st.kernel_state, "enabled");
    assert_eq!(st.kernel_ops, Some("bpfland".to_string()));
    assert!(st.healthy);
    assert!(st.managed_pid.is_some());

    // 4. Apply same policy again -> held
    let raw_held = r#"{"schema_version":1,"request_id":"req-held-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"scx_bpfland","expires_at_unix_ms":9999999999999}}"#;
    let resp_held = executor.process_raw_request(raw_held);
    assert!(resp_held.ok);
    let act_held: ActionResult = serde_json::from_value(resp_held.result.unwrap()).unwrap();
    assert_eq!(act_held.status, "held");
    assert_eq!(act_held.actual_policy, "scx_bpfland");
    assert!(act_held.verified);

    // Clean up
    executor.clean_up();
}

#[test]
fn test_expected_policy_conflict_and_deadline_exceeded() {
    let (mut executor, _dir) = create_simulated_executor();

    // Expected policy mismatch
    let raw_mismatch = r#"{"schema_version":1,"request_id":"req-mis-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"scx_rusty","expires_at_unix_ms":9999999999999}}"#;
    let resp_mis = executor.process_raw_request(raw_mismatch);
    assert!(!resp_mis.ok);
    assert_eq!(resp_mis.error.as_ref().unwrap().code, "STATE_CONFLICT");

    // Deadline expired in the past
    let raw_expired = r#"{"schema_version":1,"request_id":"req-exp-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":1000}}"#;
    let resp_exp = executor.process_raw_request(raw_expired);
    assert!(!resp_exp.ok);
    assert_eq!(resp_exp.error.as_ref().unwrap().code, "DEADLINE_EXCEEDED");

    // Unknown policy
    let raw_unknown = r#"{"schema_version":1,"request_id":"req-unk-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"nonexistent_policy","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_unk = executor.process_raw_request(raw_unknown);
    assert!(!resp_unk.ok);
    assert_eq!(resp_unk.error.as_ref().unwrap().code, "POLICY_NOT_FOUND");
}

#[test]
fn test_rollback_and_shutdown() {
    let (mut executor, _dir) = create_simulated_executor();

    // Apply scx_bpfland
    let raw_app = r#"{"schema_version":1,"request_id":"req-app-2","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_app = executor.process_raw_request(raw_app);
    assert!(resp_app.ok);

    // Rollback
    let raw_rb = r#"{"schema_version":1,"request_id":"req-rb-1","operation":"rollback","timeout_ms":5000,"params":{"expected_policy":"scx_bpfland","expires_at_unix_ms":9999999999999}}"#;
    let resp_rb = executor.process_raw_request(raw_rb);
    assert!(resp_rb.ok);
    let act_rb: ActionResult = serde_json::from_value(resp_rb.result.unwrap()).unwrap();
    assert_eq!(act_rb.status, "rolled_back");
    assert_eq!(act_rb.actual_policy, "default");
    assert!(act_rb.verified);
    assert_eq!(act_rb.managed_pid, None);

    // Shutdown
    let raw_sd = r#"{"schema_version":1,"request_id":"req-sd-1","operation":"shutdown","timeout_ms":5000,"params":{}}"#;
    let resp_sd = executor.process_raw_request(raw_sd);
    assert!(resp_sd.ok);
    let sd_res: ShutdownResult = serde_json::from_value(resp_sd.result.unwrap()).unwrap();
    assert!(sd_res.shutdown);
}

#[test]
fn test_deduplication_cache_identical_vs_mismatch() {
    let (mut executor, _dir) = create_simulated_executor();

    let raw_req = r#"{"schema_version":1,"request_id":"id-dedup-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp1 = executor.process_raw_request(raw_req);
    assert!(resp1.ok);

    // Identical request payload with same request_id -> identical cached response
    let resp2 = executor.process_raw_request(raw_req);
    assert_eq!(resp1, resp2);

    // Different request payload with same request_id -> DUPLICATE_REQUEST_MISMATCH error
    let raw_different = r#"{"schema_version":1,"request_id":"id-dedup-1","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp3 = executor.process_raw_request(raw_different);
    assert!(!resp3.ok);
    assert_eq!(
        resp3.error.as_ref().unwrap().code,
        "DUPLICATE_REQUEST_MISMATCH"
    );

    executor.clean_up();
}

#[test]
fn test_start_failure_triggers_recovery_rollback() {
    let (mut executor, _dir) = create_simulated_executor();

    // Apply scx_bpfland first
    let raw_app = r#"{"schema_version":1,"request_id":"req-app-3","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_app = executor.process_raw_request(raw_app);
    assert!(resp_app.ok);

    // Now attempt to apply scx_failing (configured to simulate failure)
    let raw_fail = r#"{"schema_version":1,"request_id":"req-fail-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_failing","expected_policy":"scx_bpfland","expires_at_unix_ms":9999999999999}}"#;
    let resp_fail = executor.process_raw_request(raw_fail);
    assert!(resp_fail.ok);

    let act_fail: ActionResult = serde_json::from_value(resp_fail.result.unwrap()).unwrap();
    // "新策略加载失败即使恢复成功也不能返回 applied。"
    assert_eq!(act_fail.status, "failed");
    assert!(!act_fail.verified);
    assert!(act_fail.error.is_some());
    assert!(act_fail.recovery.is_some());

    let recovery = act_fail.recovery.unwrap();
    assert_eq!(recovery.status, "rolled_back");
    assert_eq!(recovery.actual_policy, "scx_bpfland");
    assert!(recovery.verified);

    // Current policy restored to scx_bpfland
    assert_eq!(executor.current_policy, "scx_bpfland");

    executor.clean_up();
}
