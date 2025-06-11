import sys
from src.build_log_processor import BuildLogProcessor
from src.log_summarizer import search_errors_in_log

error_identifiers = ["error", "failure", "exception", "fatal", "panic", "failed"]

log_file_states = {
    "steps" : {
        "INSTALL" :  r"(?i)running step (.*\b(install|deploy)[\w-]+)",
        "WORKLOAD" : r"(?i)running step ((?!.*\b(install|deploy|orion)\w*)[\w-]+)",
        "ORION" : r"(?i)running step (.*\b(orion)[\w-]+)"
    },
    "initial_state": "CONFIG"
}


workload_names = {
    "workload_1",
    "workload_2"
}
def process_build_log(file_path: str):

   
    log_processor = BuildLogProcessor(log_file_states["initial_state"], error_identifiers)

    for state in log_file_states["steps"]:
        log_processor.add_state_type(state, log_file_states["steps"][state])

    results =  log_processor.run(open(file_path, "r"))
    if "potential_error" in results:
        for error_occurrence in results["potential_error"]:
            error_context = get_handler(error_occurrence["step_type"])(error_occurrence)
            yield error_context
            

def handle_config_error(error_context: dict): 

    full_errors_list = search_errors_in_log(file_content =error_context["context_lines"].split("\n"))
    return full_errors_list, error_context["step_type"], error_context["step_name"], error_context["step_line_num"]

def handle_install_error(error_context: dict): 

    full_errors_list = search_errors_in_log(file_content =error_context["context_lines"].split("\n"))
    return full_errors_list, error_context["step_type"], error_context["step_name"], error_context["step_line_num"]
 
def handle_workload_error(error_context: dict): 
    
    full_errors_list = search_errors_in_log(file_content =error_context["context_lines"].split("\n"))
    return full_errors_list, error_context["step_type"], error_context["step_name"], error_context["step_line_num"]

def handle_orion_error(error_context: dict): 

    full_errors_list = search_errors_in_log(file_content =error_context["context_lines"].split("\n"))
    return full_errors_list, error_context["step_type"], error_context["step_name"], error_context["step_line_num"]


def handle_other(error_context: dict): 

    full_errors_list = search_errors_in_log(file_content =error_context["context_lines"].split("\n"))
    return full_errors_list, error_context["step_type"], error_context["step_name"], error_context["step_line_num"]


def get_handler(error_type: str):
        handlers = {
            "CONFIG" : handle_config_error,
            "INSTALL" : handle_install_error,   
            "WORKLOAD" : handle_workload_error,
            "ORION" : handle_orion_error
        }
        return handlers.get(error_type.upper(), handle_other)


def is_workload(error_step_name: str):
    return error_step_name in workload_names



