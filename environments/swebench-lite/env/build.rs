use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    unsafe {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    // Reuse the shared environment contract so the executor decodes exactly the
    // ExecuteRequest the manager forwards over vsock. We only need the messages
    // (not the tonic service), so compile with prost-build to keep the in-VM
    // binary lean.
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let proto_root = manifest_dir.join("../../proto");
    let proto_file = proto_root.join("grl/environment/v1/environment.proto");

    println!("cargo:rerun-if-changed={}", proto_file.display());

    prost_build::compile_protos(&[proto_file], &[proto_root])?;

    Ok(())
}
