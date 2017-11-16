"""
Config required to execute a script
"""
class Config(dict):
    """
    Initialize default and job specific config
    """
    def __init__(self, job_envs_path):
        dict.__init__(self)
        self['CONSOLE_BUFFER_LENGTH'] = 20
        self['CONSOLE_FLUSH_INTERVAL_SECONDS'] = 3
        with open(job_envs_path) as job_envs:
            for env in job_envs:
                key, value = env.split('=', 1)
                value = value.strip()
                if value[0] == '"' and value[-1] == '"':
                    value = value[1:-1]
                if value[0] == "'" and value[-1] == "'":
                    value = value[1:-1]
                self[key] = value
