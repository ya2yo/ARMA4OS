use arma_executor::config::ExecutorConfig;
use arma_executor::executor::ArmaExecutor;
use arma_executor::protocol::{ActionResult, StatusResult};
use std::fs;
use tempfile::tempdir;

fn setup_fake_sysfs() -> (tempfile::TempDir, tempfile::TempDir) {
    let sysfs_dir = tempdir().unwrap();
    let state_dir = tempdir().unwrap();

    // Create root/ops and state files
    let root_dir = sysfs_dir.path().join("root");
    fs::create_dir_all(&root_dir).unwrap();
    fs::write(sysfs_dir.path().join("state"), "disabled\n").unwrap();
    fs::write(root_dir.join("ops"), "\n").unwrap();

    (sysfs_dir, state_dir)
}

#[test]
fn test_real_backend_status_disabled() {
    let (sysfs_dir, state_dir) = setup_fake_sysfs();

    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "start_timeout_ms": 200,
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

    let raw_status = r#"{"schema_version":1,"request_id":"req-st-real","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp = executor.process_raw_request(raw_status);

    assert!(resp.ok);
    let st: StatusResult = serde_json::from_value(resp.result.unwrap()).unwrap();
    assert_eq!(st.policy_id, "default");
    assert_eq!(st.kernel_state, "disabled");
    assert_eq!(st.kernel_ops, None);
    assert_eq!(st.managed_pid, None);
    assert!(st.healthy);
    assert_eq!(st.backend, "real");
}

#[test]
fn test_real_backend_external_scheduler_conflict() {
    let (sysfs_dir, state_dir) = setup_fake_sysfs();

    // Write external active scheduler into fake sysfs
    fs::write(sysfs_dir.path().join("state"), "enabled\n").unwrap();
    fs::write(
        sysfs_dir.path().join("root").join("ops"),
        "external_scheduler_ops\n",
    )
    .unwrap();

    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "start_timeout_ms": 200,
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

    // 1. Status must report external and healthy=false
    let raw_status = r#"{"schema_version":1,"request_id":"req-st-ext","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp_st = executor.process_raw_request(raw_status);
    assert!(resp_st.ok);
    let st: StatusResult = serde_json::from_value(resp_st.result.unwrap()).unwrap();
    assert_eq!(st.policy_id, "external");
    assert_eq!(st.kernel_state, "enabled");
    assert_eq!(st.kernel_ops, Some("external_scheduler_ops".to_string()));
    assert!(!st.healthy);

    // 2. Mutation must fail-closed!
    let raw_apply = r#"{"schema_version":1,"request_id":"req-app-conflict","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_app = executor.process_raw_request(raw_apply);
    assert!(!resp_app.ok);
    assert_eq!(
        resp_app.error.as_ref().unwrap().code,
        "EXTERNAL_SCHEDULER_CONFLICT"
    );
}

#[test]
fn test_real_backend_start_verification_and_timeout() {
    let (sysfs_dir, state_dir) = setup_fake_sysfs();

    // The command is /usr/bin/sleep 86400, but sysfs will NEVER become "enabled"
    let json = format!(
        r#"{{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "{state}",
            "sysfs_root": "{sysfs}",
            "start_timeout_ms": 100,
            "stop_timeout_ms": 100,
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

    // Apply should fail due to start verification timeout, and rollback to default
    let raw_apply = r#"{"schema_version":1,"request_id":"req-app-to","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_app = executor.process_raw_request(raw_apply);
    assert!(resp_app.ok);

    let act: ActionResult = serde_json::from_value(resp_app.result.unwrap()).unwrap();
    assert_eq!(act.status, "failed");
    assert!(!act.verified);
    assert!(act.error.as_ref().unwrap().contains("Timed out"));
    assert!(act.recovery.is_some());
    assert_eq!(act.recovery.as_ref().unwrap().actual_policy, "default");
    assert_eq!(executor.current_policy, "default");
}

#[test]
fn test_real_backend_successful_verification_and_child_death_detection() {
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

    let sysfs_path = sysfs_dir.path().to_path_buf();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(30));
        let _ = fs::write(sysfs_path.join("state"), "enabled\n");
        let _ = fs::write(sysfs_path.join("root").join("ops"), "test_ops\n");
    });

    let raw_apply = r#"{"schema_version":1,"request_id":"req-app-ok","operation":"apply","timeout_ms":5000,"params":{"policy_id":"scx_test","expected_policy":"default","expires_at_unix_ms":9999999999999}}"#;
    let resp_app = executor.process_raw_request(raw_apply);
    assert!(resp_app.ok);

    let act: ActionResult = serde_json::from_value(resp_app.result.unwrap()).unwrap();
    assert_eq!(act.status, "applied");
    assert!(act.verified);
    let child_pid = act.managed_pid.expect("Expected child PID");

    // Status query shows healthy
    let raw_status = r#"{"schema_version":1,"request_id":"req-st-ok","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp_st = executor.process_raw_request(raw_status);
    let st: StatusResult = serde_json::from_value(resp_st.result.unwrap()).unwrap();
    assert_eq!(st.policy_id, "scx_test");
    assert!(st.healthy);
    assert_eq!(st.managed_pid, Some(child_pid));

    // Now kill the child process externally to test child death detection
    unsafe {
        libc::kill(child_pid as libc::pid_t, libc::SIGKILL);
    }
    std::thread::sleep(std::time::Duration::from_millis(50));

    // Status query must now detect child death and report healthy = false
    let raw_status2 = r#"{"schema_version":1,"request_id":"req-st-dead","operation":"status","timeout_ms":5000,"params":{}}"#;
    let resp_st2 = executor.process_raw_request(raw_status2);
    let st2: StatusResult = serde_json::from_value(resp_st2.result.unwrap()).unwrap();
    assert!(!st2.healthy, "Expected healthy=false after child killed");

    executor.clean_up();
}
