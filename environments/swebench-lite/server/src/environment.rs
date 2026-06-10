use tonic::{Request, Response, Status};

use crate::pb::environment_service_server::EnvironmentService;
use crate::pb::{
    CloseRequest, CloseResponse, CreateEnvironmentRequest, CreateEnvironmentResponse,
    ExecuteRequest, ExecuteResponse, ResetRequest, ResetResponse,
};

#[derive(Debug, Default)]
pub struct EnvironmentServiceImpl;

#[tonic::async_trait]
impl EnvironmentService for EnvironmentServiceImpl {
    async fn create_environment(
        &self,
        request: Request<CreateEnvironmentRequest>,
    ) -> Result<Response<CreateEnvironmentResponse>, Status> {
        let task_id = request.into_inner().task_id;
        Err(Status::unimplemented(format!(
            "CreateEnvironment is not implemented for task {task_id}"
        )))
    }

    async fn execute(
        &self,
        request: Request<ExecuteRequest>,
    ) -> Result<Response<ExecuteResponse>, Status> {
        let request = request.into_inner();
        Ok(Response::new(ExecuteResponse {
            content: format!(
                "{} is not implemented for env {}: {}",
                request.tool_name, request.env_id, request.arguments_json
            ),
            is_error: true,
        }))
    }

    async fn reset(
        &self,
        request: Request<ResetRequest>,
    ) -> Result<Response<ResetResponse>, Status> {
        let env_id = request.into_inner().env_id;
        Err(Status::unimplemented(format!(
            "Reset is not implemented for env {env_id}"
        )))
    }

    async fn close(
        &self,
        request: Request<CloseRequest>,
    ) -> Result<Response<CloseResponse>, Status> {
        let env_id = request.into_inner().env_id;
        Err(Status::unimplemented(format!(
            "Close is not implemented for env {env_id}"
        )))
    }
}
