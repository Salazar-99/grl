#![cfg(target_os = "linux")]

use std::path::PathBuf;

use manager::catalog::TaskSpec;
use manager::pb::ExecuteRequest;
use manager::vm;

fn fixture_spec(root: &std::path::Path) -> TaskSpec {
    TaskSpec {
        initial_messages_json: "[]".into(),
        tools_json: "[]".into(),
        split: "conformance".into(),
        base_image: "images/base.squashfs".into(),
        task_image: "images/task.squashfs".into(),
        environment_image: root.join("active/environment.squashfs"),
    }
}

async fn execute(vm: &vm::VmHandle, tool_name: &str, arguments: &str) -> String {
    vm.executor
        .forward_execute(ExecuteRequest {
            env_id: "conformance".into(),
            tool_name: tool_name.into(),
            arguments_json: arguments.into(),
        })
        .await
        .expect("executor protocol call failed")
        .content
}

/// Runs only on a Linux host with KVM and the deterministic fixture prepared
/// by environments/conformance/build-fixture.sh.
#[tokio::test]
#[ignore = "requires /dev/kvm, Firecracker, and GRL_CONFORMANCE_ROOT"]
async fn cold_boot_snapshot_restore_pty_and_protocol() {
    assert!(PathBuf::from("/dev/kvm").exists(), "/dev/kvm is required");
    let root = PathBuf::from(
        std::env::var("GRL_CONFORMANCE_ROOT").expect("GRL_CONFORMANCE_ROOT must be set"),
    );
    unsafe {
        std::env::set_var("GRL_VM_CACHE_DIR", &root);
        std::env::set_var("GRL_VM_RUN_DIR", root.join("run"));
        std::env::set_var("GRL_VM_SNAPSHOTS", "true");
        std::env::set_var("GRL_VM_BOOT", "true");
    }
    let spec = fixture_spec(&root);

    let (_cancel_tx, cancel_rx) = tokio::sync::watch::channel(false);
    let first = vm::boot("conformance-cold", &spec, cancel_rx)
        .await
        .expect("cold boot failed");
    let evaluation = first
        .executor
        .forward_evaluate("conformance-cold")
        .await
        .expect("evaluate round trip failed");
    assert_eq!(evaluation.reward, 1.0);
    assert_eq!(execute(&first, "conformance_read", "{}").await, "missing");
    assert_eq!(
        execute(&first, "conformance_write", "first-clone").await,
        "written"
    );
    first.stop().await;

    let (_cancel_tx, cancel_rx) = tokio::sync::watch::channel(false);
    let restored = vm::boot("conformance-restored", &spec, cancel_rx)
        .await
        .expect("snapshot restore failed");
    assert_eq!(
        execute(&restored, "conformance_read", "{}").await,
        "missing",
        "the restored clone inherited another clone's writable overlay"
    );
    assert!(
        execute(&restored, "echo", r#"{"fixture":true}"#)
            .await
            .contains("minimal environment received")
    );
    restored.stop().await;
}
