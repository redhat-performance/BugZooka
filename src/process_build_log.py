import re
from typing import TextIO
class BuildLogProcessor(object):
    def __init__(self, initial_state : str, error_identifier_words : list[str]):
        self.state_handlers = []
        self.current_state = initial_state
        self.current_state_step_line = 0
        self.current_state_name = None
        self.current_line = None
        self.potential_error = False
        self.buffer = []
        self.error_identifier_words = error_identifier_words
        self.results = {"potential_error": [], "no_error": []}


    def add_state_type(self, name: str, identifier_pattern: str):
        """Register a state with a regex pattern"""
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
        for word in self.error_identifier_words:
            if word in line.lower():
                self.potential_error = True
        self.buffer.append(line)
        

    def _finalize_prev_state(self):
        context_lines = "\n".join(self.buffer)
        result_packet = {"step_type": self.current_state, "step_name": self.current_state_name, "step_line_num": self.current_state_step_line, "context_lines": context_lines} 
        if self.potential_error:
            self.results["potential_error"].append(result_packet)

        else:
            self.results["no_error"].append(result_packet)
        self.buffer = []
        self.potential_error = False
        

    def run(self, file: TextIO):

        for line_num, line in enumerate(file):
            self.process_line(line, line_num)
        self._finalize_prev_state()
        return self.results

