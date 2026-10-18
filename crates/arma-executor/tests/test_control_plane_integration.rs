use arma_executor::config::ExecutorConfig;
use arma_executor::executor::{ArmaExecutor, ManagedChild};
use serde_json::json;
use std::os::unix::process::CommandExt;
use std::process::Command;
use tempfile::tempdir;

fn request(
    executor: &mut ArmaExecutor,
    id: &str,
    op: &str,
    params: serde_json::Value,
) -> serde_json::Value {
    serde_json::to_value(executor.process_raw_request(&json!({
        "schema_version":1, "request_id":id, "operation":op, "timeout_ms":1000, "params":params
    }).to_string())).unwrap()
}

#[test]
fn monitoring_cannot_exhaust_action_cache_and_rollback_does_not_toggle() {
    let root = tempdir().unwrap();
    let config = ExecutorConfig::from_json_str(
        &json!({
            "schema_version":1, "backend":"simulated", "state_dir":root.path(),
            "policies":[{"id":"default","command":[]},
            {"id":"expert","command":["/bin/sleep","60"],"kernel_ops":"expert"}]
        })
        .to_string(),
    )
    .unwrap();
    let mut executor = ArmaExecutor::new(config);
    for i in 0..10010 {
        assert!(
            request(&mut executor, &format!("read-{i}"), "status", json!({}))["ok"]
                .as_bool()
                .unwrap()
        );
    }
    assert!(executor.request_cache.is_empty());
    let p = |expected, policy| json!({"expected_policy":expected,"policy_id":policy,"expires_at_unix_ms":9999999999999u64});
    assert_eq!(
        request(&mut executor, "apply", "apply", p("default", "expert"))["result"]["status"],
        "applied"
    );
    assert_eq!(
        request(
            &mut executor,
            "rollback-1",
            "rollback",
            json!({"expected_policy":"expert","expires_at_unix_ms":9999999999999u64})
        )["result"]["actual_policy"],
        "default"
    );
    let again = request(
        &mut executor,
        "rollback-2",
        "rollback",
        json!({"expected_policy":"default","expires_at_unix_ms":9999999999999u64}),
    );
    assert_eq!(again["result"]["actual_policy"], "default");
    assert_eq!(again["result"]["status"], "held");
}

#[test]
fn kernel_detached_owned_loader_can_be_reset_without_touching_external_processes() {
    let root = tempdir().unwrap();
    std::fs::write(root.path().join("state"), "disabled\n").unwrap();
    let config = ExecutorConfig::from_json_str(
        &json!({
            "schema_version":1,"backend":"real","state_dir":root.path(),"sysfs_root":root.path(),
            "policies":[{"id":"default","command":[]},
            {"id":"expert","command":["/bin/sleep","60"],"kernel_ops":"expert"}]
        })
        .to_string(),
    )
    .unwrap();
    let child = Command::new("/bin/sleep")
        .arg("60")
        .process_group(0)
        .spawn()
        .unwrap();
    let pid = child.id();
    let mut executor = ArmaExecutor::new(config);
    executor.current_policy = "expert".into();
    executor.managed_child = Some(ManagedChild {
        child,
        policy_id: "expert".into(),
        expected_ops: Some("expert".into()),
        pid,
        pgid: pid as libc::pid_t,
        started_at_unix_ms: 0,
    });
    let status = executor.status();
    assert!(!status.healthy);
    let response = request(
        &mut executor,
        "reset",
        "apply",
        json!({"expected_policy":status.policy_id,
        "policy_id":"default","expires_at_unix_ms":9999999999999u64}),
    );
    assert_eq!(response["result"]["actual_policy"], "default");
    assert_eq!(response["result"]["verified"], true);
    assert!(executor.managed_child.is_none());
    assert!(executor.status().healthy);
}
