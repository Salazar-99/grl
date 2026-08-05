//! Static, single-turn shaped-reward environment.

use serde::Deserialize;
use serde_json::json;

#[derive(Debug, Deserialize)]
struct Payload {
    input: String,
    target: String,
}

#[derive(Clone)]
pub struct Task {
    input: String,
    target: String,
}

pub fn initialize(payload_json: &str) -> Result<(Task, String, String), String> {
    let payload: Payload =
        serde_json::from_str(payload_json).map_err(|e| format!("malformed task payload: {e}"))?;
    let input: Vec<char> = payload.input.chars().collect();
    let target: Vec<char> = payload.target.chars().collect();
    if input.len() != 3
        || target.len() != 3
        || target != input.iter().rev().copied().collect::<Vec<_>>()
    {
        return Err(
            "input and target must be exactly three characters and target must be input reversed"
                .into(),
        );
    }
    let prompt = format!(
        "Reverse the string: {}\nRespond with the reversed string in <answer>...</answer> tags.",
        payload.input
    );
    Ok((
        Task {
            input: payload.input,
            target: payload.target,
        },
        prompt,
        "[]".into(),
    ))
}

pub fn evaluate(task: &Task, response: &str) -> (f64, String) {
    let received = response.trim().to_string();
    let received_chars: Vec<char> = received.chars().collect();
    let target: Vec<char> = task.target.chars().collect();
    let matches: Vec<bool> = (0..3)
        .map(|i| received_chars.get(i) == target.get(i))
        .collect();
    let matched_positions = matches.iter().filter(|v| **v).count();
    let detail = json!({"input": task.input, "target": task.target, "received": received,
        "matches": matches, "matched_positions": matched_positions,
        "format_valid": received_chars.len() == 3})
    .to_string();
    (matched_positions as f64, detail)
}

/// Extract the final answer field from an assistant response.
///
/// Reasoning may precede the tag, but the answer itself must be enclosed in
/// `<answer>...</answer>`. Selecting the last opening tag makes a later final
/// answer unambiguous if a model happens to use an earlier tag while reasoning.
fn extract_tagged_answer(response: &str) -> Option<&str> {
    let opening = "<answer>";
    let closing = "</answer>";
    let start = response.rfind(opening)? + opening.len();
    let content_and_suffix = &response[start..];
    let end = content_and_suffix.find(closing)?;
    Some(content_and_suffix[..end].trim())
}

/// Evaluate the JSON envelope used by the host-facing EvaluateRequest.
/// Invalid or absent assistant content is a valid zero-reward answer.
pub fn evaluate_final_message_json(task: &Task, final_message_json: &str) -> (f64, String) {
    let response = serde_json::from_str::<serde_json::Value>(final_message_json)
        .ok()
        .and_then(|value| {
            value.as_str().map(str::to_owned).or_else(|| {
                value
                    .get("content")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned)
            })
        });
    match response.and_then(|response| extract_tagged_answer(&response).map(str::to_owned)) {
        Some(answer) => evaluate(task, &answer),
        None => evaluate(task, ""),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shaped_positions() {
        let (task, prompt, tools) = initialize(r#"{"input":"abc","target":"cba"}"#).unwrap();
        assert_eq!(
            prompt,
            "Reverse the string: abc\nRespond with the reversed string in <answer>...</answer> tags."
        );
        assert_eq!(tools, "[]");
        assert_eq!(evaluate(&task, "cxa").0, 2.0);
        assert_eq!(evaluate(&task, " cba ").0, 3.0);
        assert_eq!(evaluate(&task, "cba!").0, 3.0);
        assert_eq!(
            evaluate_final_message_json(
                &task,
                r#"{"role":"assistant","content":"reasoning\n<answer>xba</answer>"}"#
            )
            .0,
            2.0
        );
        assert_eq!(
            evaluate_final_message_json(&task, r#"{"content":"<answer>cba</answer>"}"#).0,
            3.0
        );
        assert_eq!(
            evaluate_final_message_json(&task, r#"{"content":"cba"}"#).0,
            0.0
        );
    }
    #[test]
    fn rejects_bad_payloads() {
        assert!(initialize(r#"{"input":"ab","target":"ba"}"#).is_err());
        assert!(initialize(r#"{"input":"abc","target":"abc"}"#).is_err());
    }
}
