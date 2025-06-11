import re

error_identifiers = ["error", "failure", "exception", "fatal", "panic", "failed"]
class BuildLogProcessor(object):
    def __init__(self):
        self.state_handlers = []
        self.current_state = "CONFIG" # initial stateless state
        self.current_state_step_line = 0
        self.current_state_name = None
        self.current_line = None
        self.potential_error = False
        self.buffer = []
        self.results = {"potential_errors": {}, "no_errors": {}}

    def add_step_type(self, name: str, identifier_pattern: re.Pattern):
        """Register a state with a regex pattern, optional enter action, and multiline flag."""
        self.state_handlers.append({
            'name': name,
            'pattern': re.compile(identifier_pattern)        })

    def process_line(self, line: str, line_num: int):
        self.current_line = line
        line = line.strip()
        # If started, check for state matches
        for handler in self.state_handlers:
            if handler['pattern'].search(line):
                
                self._finalize_prev_state()
                self.buffer.append(line)
                self.current_state = handler['name']
                self.current_state_name = handler['pattern'].search(line).group(1)
                self.current_state_step_line = line_num
                self.current_line = line
                return 

        # If no state matched, and we are buffering, keep collecting
        for word in error_identifiers:
            if word in line.lower():
                self.potential_error = True
        self.buffer.append(line)
        

    def _finalize_prev_state(self):
        context_lines = "\n".join(self.buffer)
        if self.potential_error:
            self.results["potential_errors"][self.current_state, self.current_state_name, self.current_state_step_line] = context_lines
        else:
            self.results["no_errors"][self.current_state, self.current_state_name, self.current_state_step_line] = context_lines
        self.buffer = []
        self.potential_error = False
        

    def run(self, lines: list[str]):
        for line_num, line in enumerate(lines):
            self.process_line(line, line_num)
        self._finalize_prev_state()
        return self.results


# log_processor = BuildLogProcessor()
# log_processor.add_step_type("INSTALL", r"(?i)running step (.*\b(install|deploy)[\w-]+)")
# log_processor.add_step_type("WORKLOAD", r"(?i)running step ((?!.*\b(install|deploy|orion)\w*)[\w-]+)")
# log_processor.add_step_type("ORION", r"(?i)running step (.*\b(orion)[\w-]+)")


# log_data = open("./orion/build-log0.txt").readlines()

# results = fsm.run(log_data)

