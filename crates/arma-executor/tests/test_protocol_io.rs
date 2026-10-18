use arma_executor::config::ExecutorConfig;
use arma_executor::executor::ArmaExecutor;
use arma_executor::protocol::ProtocolResponse;
use arma_executor::run_executor_sync;
use std::io::Cursor;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use tempfile::tempdir;

#[test]
fn test_io_stream_session() {
    let dir = tempdir().unwrap();
    let json_cfg = format!(
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

    let config = ExecutorConfig::from_json_str(&json_cfg).unwrap();
    let executor = ArmaExecutor::new(config);

    // Multi-line input stream simulating stdin
    let input = concat!(
        "{\"schema_version\":1,\"request_id\":\"line-1\",\"operation\":\"status\",\"timeout_ms\":5000,\"params\":{}}\n",
        "{\"schema_version\":1,\"request_id\":\"line-2\",\"operation\":\"apply\",\"timeout_ms\":5000,\"params\":{\"policy_id\":\"scx_bpfland\",\"expected_policy\":\"default\",\"expires_at_unix_ms\":9999999999999}}\n",
        "{\"schema_version\":1,\"request_id\":\"line-3\",\"operation\":\"rollback\",\"timeout_ms\":5000,\"params\":{\"expected_policy\":\"scx_bpfland\",\"expires_at_unix_ms\":9999999999999}}\n",
        "{\"schema_version\":1,\"request_id\":\"line-4\",\"operation\":\"shutdown\",\"timeout_ms\":5000,\"params\":{}}\n"
    );

    let reader = Cursor::new(input.as_bytes());
    let mut writer = Vec::new();
    let shutdown_flag = Arc::new(AtomicBool::new(false));

    let result = run_executor_sync(executor, reader, &mut writer, shutdown_flag);
    assert!(result.is_ok());

    let output_str = String::from_utf8(writer).unwrap();
    let lines: Vec<&str> = output_str.trim().split('\n').collect();
    assert_eq!(lines.len(), 4);

    let resp1: ProtocolResponse = serde_json::from_str(lines[0]).unwrap();
    assert_eq!(resp1.request_id, "line-1");
    assert!(resp1.ok);

    let resp2: ProtocolResponse = serde_json::from_str(lines[1]).unwrap();
    assert_eq!(resp2.request_id, "line-2");
    assert!(resp2.ok);

    let resp3: ProtocolResponse = serde_json::from_str(lines[2]).unwrap();
    assert_eq!(resp3.request_id, "line-3");
    assert!(resp3.ok);

    let resp4: ProtocolResponse = serde_json::from_str(lines[3]).unwrap();
    assert_eq!(resp4.request_id, "line-4");
    assert!(resp4.ok);
}

#[test]
fn test_io_stream_malformed_input() {
    let dir = tempdir().unwrap();
    let json_cfg = format!(
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

    let config = ExecutorConfig::from_json_str(&json_cfg).unwrap();
    let executor = ArmaExecutor::new(config);

    let input = "not a valid json\n";
    let reader = Cursor::new(input.as_bytes());
    let mut writer = Vec::new();
    let shutdown_flag = Arc::new(AtomicBool::new(false));

    let result = run_executor_sync(executor, reader, &mut writer, shutdown_flag);
    assert!(result.is_ok());

    let output_str = String::from_utf8(writer).unwrap();
    let resp: ProtocolResponse = serde_json::from_str(output_str.trim()).unwrap();
    assert!(!resp.ok);
    assert_eq!(resp.error.unwrap().code, "INVALID_REQUEST");
}
