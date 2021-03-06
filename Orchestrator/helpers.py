

def validate_docker_image(data):
    return True

def find_missing_keys(data, keys):
    return list(filter(lambda x: x not in data.keys(), keys))

def validate_mission(data):
    if not isinstance(data, dict):
        return False
    keys = [
        'waypoints',
        'operation_id',
        'group_id',
        'delivery_id',
        'control_area_id',
        'operation_reference_number',
        'drone_id',
        'drone_registration_number',
        'cvms_auth_token',
        'dis_auth_token',
        'dis_refresh_token',
        'thermal_model_timestep',
        'aeroacoustic_model_timestep',
        'drone_config_file',
        'g_acceleration',
        'initial_lon_lat_alt',
        'final_lon_lat_alt',
        'effective_start_time']
    missing_keys = find_missing_keys(data, keys)
    if len(missing_keys) > 0:
        print(f'Missing keys: {missing_keys}')
        return False
    return True

def validate_payload(payload):
    if payload is None:
        return False
    return not (payload is None or 'docker_img' not in payload or 'mission' not in payload) and \
    (validate_docker_image(payload['docker_img'])) and \
    (validate_mission(payload['mission']))
