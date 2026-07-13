use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    unsafe {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../proto");
    prost_build::compile_protos(
        &[root.join("grl/environment/v1/environment.proto")],
        &[root],
    )?;
    Ok(())
}
