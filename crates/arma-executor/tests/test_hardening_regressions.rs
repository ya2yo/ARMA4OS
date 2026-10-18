use arma_executor::config::ExecutorConfig;
use arma_executor::executor::{now_unix_ms, ArmaExecutor, MAX_CACHE_SIZE};
use arma_executor::protocol::{ActionResult, ProtocolResponse, StatusResult};
use arma_executor::run_executor_sync;
use std::fs;
use std::io::{Cursor, Write};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::Instant;
use tempfile::tempdir;

fn setup_fake_sysfs() -> (tempfile::TempDir, tempfile::TempDir) {
    let sysfs_dir = tempdir().unwrap();
    let state_dir = tempdir().unwrap();

    let root_dir = sysfs_dir.path().join("root");
    fs::create_dir_all(&root_dir).unwrap();
    fs::write(sysfs_dir.path().join("state"), "disabled\n").unwrap();
    fs::write(root_dir.join("ops"), "\n").unwrap();

    (sysfs_dir, state_dir)
}

// 1. Missing preconditions and unknown fields validation
#[test]
fn test_regression_missing_preconditions_and_unknown_fields() {
    let dir = tempdir().unwrap();
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "{dir}",
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"], "kernel_ops": "bpfland"}}
            ]
        }}"#,
        dir = dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    // Missing expected_policy
    let raw_missing_exp = r#"{"schema_version":1,"request_id":"pre-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expires_at_unix_ms":9999999999999}}"#;
    let resp1 = executor.process_raw_request(raw_missing_exp);
    assert!(!resp1.ok);
    assert_eq!(resp1.error.as_ref().unwrap().code, "INVALID_REQUEST");
    assert!(resp1
        .error
        .as_ref()
        .unwrap()
        .message
        .contains("expected_policy"));

    // Missing expires_at_unix_ms
    let raw_missing_exp_ms = r#"{"schema_version":1,"request_id":"pre-2","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default"}}"#;
    let resp2 = executor.process_raw_request(raw_missing_exp_ms);
    assert!(!resp2.ok);
    assert_eq!(resp2.error.as_ref().unwrap().code, "INVALID_REQUEST");
    assert!(resp2
        .error
        .as_ref()
        .unwrap()
        .message
        .contains("expires_at_unix_ms"));

    // Expiry in past / now
    let now = now_unix_ms();
    let raw_expired = format!(
        r#"{{"schema_version":1,"request_id":"pre-3","operation":"apply","timeout_ms":5000,"params":{{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":{now}}}}}"#
    );
    let resp3 = executor.process_raw_request(&raw_expired);
    assert!(!resp3.ok);
    assert_eq!(resp3.error.as_ref().unwrap().code, "DEADLINE_EXCEEDED");

    // Invalid timeout_ms: 0
    let raw_zero_to = r#"{"schema_version":1,"request_id":"pre-4","operation":"apply","timeout_ms":0,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp4 = executor.process_raw_request(raw_zero_to);
    assert!(!resp4.ok);
    assert_eq!(resp4.error.as_ref().unwrap().code, "INVALID_REQUEST");

    // Invalid timeout_ms: > 120000
    let raw_large_to = r#"{"schema_version":1,"request_id":"pre-5","operation":"apply","timeout_ms":120001,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp5 = executor.process_raw_request(raw_large_to);
    assert!(!resp5.ok);
    assert_eq!(resp5.error.as_ref().unwrap().code, "INVALID_REQUEST");

    // Unknown field in top-level request
    let raw_unknown_top = r#"{"schema_version":1,"request_id":"pre-6","operation":"status","timeout_ms":5000,"params":{},"rogue_field":123}"#;
    let resp6 = executor.process_raw_request(raw_unknown_top);
    assert!(!resp6.ok);
    assert_eq!(resp6.error.as_ref().unwrap().code, "INVALID_REQUEST");

    // Unknown field in apply params
    let raw_unknown_params = r#"{"schema_version":1,"request_id":"pre-7","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999,"unexpected":true}}"#;
    let resp7 = executor.process_raw_request(raw_unknown_params);
    assert!(!resp7.ok);
    assert_eq!(resp7.error.as_ref().unwrap().code, "INVALID_REQUEST");

    // Unknown field in status params
    let raw_unknown_status = r#"{"schema_version":1,"request_id":"pre-8","operation":"status","timeout_ms":5000,"params":{"unexpected":1}}"#;
    let resp8 = executor.process_raw_request(raw_unknown_status);
    assert!(!resp8.ok);
    assert_eq!(resp8.error.as_ref().unwrap().code, "INVALID_REQUEST");
}

// 2. Unsupported sysfs: refuse dry_run and held verified:true
#[test]
fn test_regression_unsupported_sysfs_refuses_dry_run_and_held() {
    let state_dir = tempdir().unwrap();
    // Non-existent sysfs path
    let nonexistent_sysfs = state_dir.path().join("does_not_exist_sysfs");

    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_test", "command": ["/usr/bin/sleep", "86400"], "kernel_ops": "test_ops"}}
            ]
        }}"#,
        state = state_dir.path().to_str().unwrap(),
        sysfs = nonexistent_sysfs.to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    // Status query must report external, unsupported, healthy=false
    let raw_status = r#"{"schema_version":1,"request_id":"unsup-st","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp_st = executor.process_raw_request(raw_status);
    assert!(resp_st.ok);
    let st: StatusResult = serde_json::from_value(resp_st.result.unwrap()).unwrap();
    assert_eq!(st.policy_id, "external");
    assert_eq!(st.kernel_state, "unsupported");
    assert!(!st.healthy);

    // Dry-run must be refused when kernel state unsupported
    let raw_dry = r#"{"schema_version":1,"request_id":"unsup-dry","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999,"dry_run":true}}"#;
    let resp_dry = executor.process_raw_request(raw_dry);
    assert!(!resp_dry.ok);
    assert!(
        resp_dry.error.as_ref().unwrap().code == "STATE_CONFLICT"
            || resp_dry.error.as_ref().unwrap().code == "EXTERNAL_SCHEDULER_CONFLICT"
    );

    // Mutation must be refused
    let raw_app = r#"{"schema_version":1,"request_id":"unsup-app","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999,"dry_run":false}}"#;
    let resp_app = executor.process_raw_request(raw_app);
    assert!(!resp_app.ok);
}

// 3. Stale internal state: verify expected_policy against fresh status() not internal current_policy
#[test]
fn test_regression_stale_internal_state_vs_fresh_status() {
    let (sysfs_dir, state_dir) = setup_fake_sysfs();

    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "start_timeout_ms": 500,
            "stop_timeout_ms": 200,
            "poll_interval_ms": 20,
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_test", "command": ["/usr/bin/sleep", "86400"], "kernel_ops": "test_ops"}}
            ]
        }}"#,
        state = state_dir.path().to_str().unwrap(),
        sysfs = sysfs_dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    // 1. Simulate sysfs becoming enabled
    let sysfs_path = sysfs_dir.path().to_path_buf();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(30));
        let _ = fs::write(sysfs_path.join("state"), "enabled\n");
        let _ = fs::write(sysfs_path.join("root").join("ops"), "test_ops\n");
    });

    let raw_apply = r#"{"schema_version":1,"request_id":"stale-app","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp = executor.process_raw_request(raw_apply);
    assert!(resp.ok);
    assert_eq!(executor.current_policy, "scx_test");

    let pid = executor.managed_child.as_ref().unwrap().pid;

    // 2. Kill the child process externally
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGKILL);
    }
    std::thread::sleep(std::time::Duration::from_millis(50));

    // Also externally reset sysfs state to disabled
    fs::write(sysfs_dir.path().join("state"), "disabled\n").unwrap();

    // 3. Client assumes current_policy is still "scx_test" and sends expected_policy: "scx_test"
    let raw_stale_exp = r#"{"schema_version":1,"request_id":"stale-check","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"scx_test","expires_at_unix_ms":9999999999999}}"#;
    let resp_stale = executor.process_raw_request(raw_stale_exp);
    // MUST FAIL with STATE_CONFLICT because fresh status().policy_id is "default", NOT "scx_test"!
    assert!(!resp_stale.ok);
    assert_eq!(resp_stale.error.as_ref().unwrap().code, "STATE_CONFLICT");

    // 4. Safe reset to default
    let raw_reset = r#"{"schema_version":1,"request_id":"stale-reset","operation":"apply","timeout_ms":5000,"params":{"policy_id":"default","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_reset = executor.process_raw_request(raw_reset);
    assert!(resp_reset.ok);
    let st = executor.status();
    assert_eq!(st.policy_id, "default");
    assert!(st.healthy);
}

// 4. Managed child still alive but sysfs disabled: refuse dry_run and held verified:true
#[test]
fn test_regression_managed_child_alive_but_sysfs_disabled() {
    let (sysfs_dir, state_dir) = setup_fake_sysfs();

    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "start_timeout_ms": 500,
            "stop_timeout_ms": 200,
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_test", "command": ["/usr/bin/sleep", "86400"], "kernel_ops": "test_ops"}}
            ]
        }}"#,
        state = state_dir.path().to_str().unwrap(),
        sysfs = sysfs_dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    // Apply successfully
    let sysfs_path = sysfs_dir.path().to_path_buf();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(30));
        let _ = fs::write(sysfs_path.join("state"), "enabled\n");
        let _ = fs::write(sysfs_path.join("root").join("ops"), "test_ops\n");
    });

    let raw_apply = r#"{"schema_version":1,"request_id":"alive-app","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp = executor.process_raw_request(raw_apply);
    assert!(resp.ok);
    assert!(executor.managed_child.is_some());

    // Invalidate sysfs behind the running child
    fs::write(sysfs_dir.path().join("state"), "disabled\n").unwrap();

    // Status query must detect child is alive while sysfs is disabled -> healthy=false, policy_id=external
    let st = executor.status();
    assert_eq!(st.kernel_state, "disabled");
    assert_eq!(st.policy_id, "external");
    assert!(!st.healthy);
    assert!(st.managed_pid.is_some());

    // Dry-run must be refused!
    let raw_dry = r#"{"schema_version":1,"request_id":"alive-dry","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"external","expires_at_unix_ms":9999999999999,"dry_run":true}}"#;
    let resp_dry = executor.process_raw_request(raw_dry);
    assert!(!resp_dry.ok);

    // Held must be refused!
    let raw_held = r#"{"schema_version":1,"request_id":"alive-held","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"scx_test","expires_at_unix_ms":9999999999999}}"#;
    let resp_held = executor.process_raw_request(raw_held);
    assert!(!resp_held.ok);

    executor.clean_up();
}

// 5. Request budget: small timeout_ms bounds configured waits and triggers recovery
#[test]
fn test_regression_request_budget_bounds_waits() {
    let (sysfs_dir, state_dir) = setup_fake_sysfs();

    // Configure executor with a long start_timeout (3000ms), but we will pass timeout_ms: 100ms
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "start_timeout_ms": 3000,
            "stop_timeout_ms": 200,
            "poll_interval_ms": 10,
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_test", "command": ["/usr/bin/sleep", "86400"], "kernel_ops": "test_ops"}}
            ]
        }}"#,
        state = state_dir.path().to_str().unwrap(),
        sysfs = sysfs_dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    let start = Instant::now();
    // small timeout_ms: 80ms
    let raw_apply = r#"{"schema_version":1,"request_id":"req-budget-1","operation":"apply","timeout_ms":80,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp = executor.process_raw_request(raw_apply);
    let elapsed = start.elapsed();

    // Must have returned quickly due to request budget, NOT waiting the full 3000ms!
    assert!(
        elapsed.as_millis() < 1000,
        "Action took too long: {:?}",
        elapsed
    );

    assert!(resp.ok);
    let act: ActionResult = serde_json::from_value(resp.result.unwrap()).unwrap();
    assert_eq!(act.status, "failed");
    assert!(!act.verified);
    // Verification failed within budget and triggered recovery
    assert!(act.recovery.is_some());
    assert_eq!(act.recovery.as_ref().unwrap().actual_policy, "default");
    assert_eq!(executor.current_policy, "default");
    // Managed child was cleanly terminated and reaped
    assert!(executor.managed_child.is_none());
}

// 6. Broken stdout cleanup: broken pipe during run_executor_sync reaps child
struct BrokenWriter {
    written: usize,
}

impl Write for BrokenWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.written += buf.len();
        if self.written > 50 {
            Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "Broken pipe on stdout",
            ))
        } else {
            Ok(buf.len())
        }
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Err(std::io::Error::new(
            std::io::ErrorKind::BrokenPipe,
            "Broken pipe on flush",
        ))
    }
}

#[test]
fn test_regression_broken_stdout_cleans_up_managed_child() {
    let dir = tempdir().unwrap();
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "{dir}",
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"], "kernel_ops": "bpfland"}}
            ]
        }}"#,
        dir = dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    // Apply scx_bpfland to spawn child
    let raw_app = r#"{"schema_version":1,"request_id":"broken-1","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp = executor.process_raw_request(raw_app);
    assert!(resp.ok);
    let child_pid = executor.managed_child.as_ref().unwrap().pid;

    // Check process is running in OS
    unsafe {
        assert_eq!(libc::kill(child_pid as libc::pid_t, 0), 0);
    }

    // Now feed a request into run_executor_sync with BrokenWriter
    let input = "{\"schema_version\":1,\"request_id\":\"broken-2\",\"operation\":\"status\",\"timeout_ms\":5000,\"params\":{}}\n";
    let reader = Cursor::new(input.as_bytes());
    let writer = BrokenWriter { written: 0 };
    let shutdown_flag = Arc::new(AtomicBool::new(false));

    let res = run_executor_sync(executor, reader, writer, shutdown_flag);
    assert!(res.is_err());

    // Give kernel a few milliseconds to reap child
    std::thread::sleep(std::time::Duration::from_millis(50));

    // Child PID must NO LONGER be alive in OS
    unsafe {
        assert_ne!(libc::kill(child_pid as libc::pid_t, 0), 0);
    }
}

// 7. Request cache bounded to MAX_CACHE_SIZE and overcapacity status remains usable
#[test]
fn test_regression_cache_capacity_and_overcapacity_status() {
    let dir = tempdir().unwrap();
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "{dir}",
            "policies": [
                {{"id": "default", "command": []}},
                {{"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"], "kernel_ops": "bpfland"}}
            ]
        }}"#,
        dir = dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let mut executor = ArmaExecutor::new(config);

    // Pre-populate cache to MAX_CACHE_SIZE
    let dummy_resp = ProtocolResponse::success("dummy".to_string(), serde_json::json!({}));
    let dummy_val = serde_json::json!({"dummy": true});
    for i in 0..MAX_CACHE_SIZE {
        executor.request_cache.insert(
            format!("cached-{}", i),
            (dummy_val.clone(), dummy_resp.clone()),
        );
    }
    assert_eq!(executor.request_cache.len(), MAX_CACHE_SIZE);

    // New mutating request must be refused with CAPACITY_EXCEEDED!
    let raw_mut = r#"{"schema_version":1,"request_id":"new-mutating","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_mut = executor.process_raw_request(raw_mut);
    assert!(!resp_mut.ok);
    assert_eq!(resp_mut.error.as_ref().unwrap().code, "CAPACITY_EXCEEDED");
    // Cache size did NOT grow or evict
    assert_eq!(executor.request_cache.len(), MAX_CACHE_SIZE);

    // Overcapacity status must remain fully usable!
    let raw_status = r#"{"schema_version":1,"request_id":"status-overcap","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp_status = executor.process_raw_request(raw_status);
    assert!(resp_status.ok);
    let st: StatusResult = serde_json::from_value(resp_status.result.unwrap()).unwrap();
    assert_eq!(st.policy_id, "default");
    // Status was not cached forever
    assert_eq!(executor.request_cache.len(), MAX_CACHE_SIZE);

    // Overcapacity shutdown must remain usable!
    let raw_sd = r#"{"schema_version":1,"request_id":"sd-overcap","operation":"shutdown","timeout_ms":5000,"params":{}}"#;
    let resp_sd = executor.process_raw_request(raw_sd);
    assert!(resp_sd.ok);
}

// 8. Input line bounded limit test
#[test]
fn test_regression_input_line_bounded() {
    let dir = tempdir().unwrap();
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "{dir}",
            "policies": [
                {{"id": "default", "command": []}}
            ]
        }}"#,
        dir = dir.path().to_str().unwrap()
    );

    let config = ExecutorConfig::from_json_str(&json).unwrap();
    let executor = ArmaExecutor::new(config);

    // Create an input line exceeding 1 MiB (1_048_576 bytes)
    let oversized = "a".repeat(1_048_576 + 10) + "\n";
    let reader = Cursor::new(oversized.as_bytes());
    let mut writer = Vec::new();
    let shutdown_flag = Arc::new(AtomicBool::new(false));

    let res = run_executor_sync(executor, reader, &mut writer, shutdown_flag);
    assert!(res.is_ok());

    let output_str = String::from_utf8(writer).unwrap();
    let resp: ProtocolResponse = serde_json::from_str(output_str.trim()).unwrap();
    assert!(!resp.ok);
    assert_eq!(resp.error.unwrap().code, "INVALID_REQUEST");
}
