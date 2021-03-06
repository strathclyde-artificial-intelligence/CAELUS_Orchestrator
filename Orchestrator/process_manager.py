import uuid
import json
import os
import time
import logging
from math import floor
from typing import Dict
from threading import Thread
from queue import Empty, PriorityQueue 
from random import random

import docker as docker_lib
from .docker_helper import get_docker
from .mongo import store_new_process, update_process_status, cleanup_dangling_processes
from .error_codes import *

class Process(Thread):
    CREATED, RUNNING, ERROR, TERMINATED, HALTED = 0,1,2,3,4
    @staticmethod
    def status_to_string(s):
        return {i:v for i,v in enumerate(['CREATED', 'RUNNING', 'ERROR', 'TERMINATED', 'HALTED'])}[s]

    def __init__(self, id, issuer_id, docker_image, mission_payload, status_update_delegate, logger=logging.getLogger()):
        super().__init__()
        self.__id = id
        self.__group_id = mission_payload['group_id']
        self.__issuer_id = issuer_id
        self.__status = Process.CREATED
        self.__mission_payload = mission_payload
        self.__docker_image = docker_image
        self.__error = None
        self.__error_code = None
        self.__created_at = time.time()
        self.__logger = logger
        self.__should_stop = False
        self.__delegate = status_update_delegate
        self.__status_message = "No message"
        
    def halt(self):
        self.__should_stop = True

    def get_issuer(self):
        return self.__issuer_id

    def get_status(self):
        return self.__status

    def get_status_string(self):
        return f'{Process.status_to_string(self.__status)} ({self.__status_message})'

    def set_status(self, s, message=None, error_code=None):
        self.__status = s
        self.__status_message = message
        self.__error_code = error_code
        self.__delegate.process_status_changed(self)
        
    def __simulate_docker(self):
        def wait():
            time.sleep(floor(10 * random()))
        t = Thread(target=wait)
        t.start()
        while t.is_alive():
            t.join(timeout=0.5)
            if self.__should_stop:
                self.__logger.info(f'Process {self} forcibly exited.')
                return Process.HALTED
        self.__logger.info(f'Process {self} terminated')
        return self.__code_to_result(0)

    def __code_to_result(self, exit_code):
        if exit_code == OK:
            return Process.TERMINATED
        elif exit_code == SIGTERM or exit_code == SIGKILL: # assume killed by user via /halt/<pid>
            return Process.HALTED
        elif exit_code == MISSION_UPLOAD_FAIL:
            self.__error = Exception('Mission upload fail.')
            return Process.ERROR
        elif exit_code == STREAM_READ_FAILURE:
            self.__error = Exception('Failed in starting up simulation stack.')
            return Process.ERROR
        elif exit_code == VEHICLE_TIMED_OUT:
            self.__error = Exception('Vehicle Mavlink connection timed out!')
            return Process.ERROR
        elif exit_code == PREMATURE_LANDING:
            self.__error = Exception('Vehicle has landed before reaching landing spot. Check vehicle configuration!')
        elif exit_code == UNKNOWN_VEHICLE:
            self.__error = Exception('Unknown vehicle model, check available vehicles.')
        elif exit_code == PX4_SIM_DESYNC:
            self.__error = Exception('PX4 simulation desync -- server may be overloaded.')
        elif exit_code == TOO_MUCH_WIND:
            self.__error = Exception('There is too much wind to fly safely.')
        return Process.ERROR

    def __run_docker_instance(self):
        def monitor(container):
            while True:
                if self.__should_stop:
                    self.__logger.info(f'Container {self.__mission_payload["operation_id"]} has been issued a forceful stop.')
                    try:
                        container.stop(timeout=5)
                        return Process.HALTED, None
                    except docker_lib.errors.APIError as e:
                        self.__logger.error(e)
                        self.__error = e
                        return Process.ERROR, UNDEFINED_ERROR
                try:
                    status = container.wait(timeout=3)
                    error = status['Error'] if 'Error' in status else None
                    error_code = status['StatusCode'] if 'StatusCode' in status else None
                    self.__logger.info(f'Container exited with status {error_code}')
                    self.__error = error
                    if 'DELETE_CONTAINERS' in os.environ and os.environ['DELETE_CONTAINERS'] == 'True':
                        self.__logger.info('Removing container {container}')
                        container.remove()
                    return self.__code_to_result(error_code), error_code if error_code not in [OK, SIGTERM, SIGKILL] else None
                except Exception:
                    pass
        container = None
        try:
            self.__logger.info(f'Spawning container for image {self.get_docker_image()}.')
            container = get_docker().client.containers.create(
                self.get_docker_image(),
                detach=True,
                auto_remove=False,
                network_mode='caelus_orchestrator_default',
                stdin_open = True, tty = True,
                environment={'PAYLOAD':json.dumps(self.__mission_payload)})
            container.start()
            self.__logger.info(f'Container {container} spawned successfully.')
            self.set_status(Process.RUNNING)
            status_code, error_code = monitor(container)
            return status_code, error_code
        except Exception as e:
            self.__logger.error(e)
            self.__error = e
            return Process.ERROR, None

    def run(self):
        try:
            self.__logger.info(f'Starting process {self} with image {self.__docker_image}')
            self.set_status(Process.RUNNING)
            status_code, error_code = self.__run_docker_instance()
            self.__logger.info(f'Returned status code: {status_code}')
            self.set_status(status_code, message=str(self.__error), error_code=error_code)
        except Exception as e:
            self.__logger.info(f'{self} errored out during startup')
            self.__logger.error(f'{e}')
            self.set_status(Process.ERROR)
            self.__error = e

    def __repr__(self) -> str:
        return f'<Process:{self.__mission_payload["operation_id"]}_{self.__created_at}>'

    def get_id(self):
        return self.__id

    def get_error(self):
        return self.__error

    def get_force_stop_condition(self):
        return self.__force_stop

    def get_docker_image(self):
        return self.__docker_image

    def get_mission_data(self):
        return self.__mission_payload

    def get_group_id(self):
        return self.__group_id

    def get_error_code(self):
        return self.__error_code

    def to_dict(self):
        return {
            'id': self.get_id(),
            'group_id': self.get_group_id(),
            'docker_image': self.get_docker_image(),
            'mission_payload': self.get_mission_data(),
            'status': self.get_status(),
            'status_str': self.get_status_string(),
            'error_code': self.get_error_code(),
            'issuer_id': self.get_issuer()
        }

class ProcessManager():

    def __init__(self, db, max_concurrent_processes = 8, logger=logging.getLogger()):
        self.__max_concurrent_processes = max_concurrent_processes
        self.__ps_running = 0
        self.__ps_queue = PriorityQueue()
        self.__active_ps: Dict[str, Process] = {}
        self.__old_ps: Dict[str, Process] = {}
        self.__database = db
        self.__monitor_thread = Thread(target=self.monitor)
        self.__monitor_thread.name = 'Process monitor'
        self.__monitor_thread.daemon = True
        self.__monitor_thread.start()
        self.__logger = logger
        cleanup_dangling_processes(db)
    
    def __new_process(self, process):
        store_new_process(self.__database, process)

    def process_status_changed(self, process):
        update_process_status(self.__database, process)

    def __start_new_process(self, docker_image, mission_payload, _id, issuer_id):
        p = Process(_id, issuer_id, docker_image, mission_payload, self, logger=self.__logger)
        p.daemon = True
        p.name = f'Simulation_{mission_payload["operation_id"]}'
        p.start()
        self.__active_ps[_id] = p
        self.__new_process(p)

    def halt_process(self, process_id):
        if process_id not in self.__active_ps:
            self.__logger.warn(f'Tried to halt a non-existing process ({process_id})')
            return False
        self.__logger.info(f'Sending force stop command for process {process_id}')
        p = self.__active_ps[process_id]
        p.halt()
        return True

    def __image_available(self, img):
        try:
            _  = get_docker().client.images.get(img)
            return True
        except Exception as e:
            return False

    def __process_with_operation_id(self, operation_id):
        for p in self.__active_ps.values():
            if p.get_mission_data()['operation_id'] == operation_id:
                return p
        return None
        
    def schedule_process(self, docker_image, mission_payload, issuer_id):
        if not self.__image_available(docker_image):
            return None
        if self.__process_with_operation_id(mission_payload['operation_id']) is not None:
            raise Exception(f'Operation {mission_payload["operation_id"]} already scheduled')

        _id = str(uuid.uuid4())
        effective_start_time = mission_payload['effective_start_time']
        self.__logger.info(f'Enqueueing new process (docker_img: {docker_image}, mission: {mission_payload["operation_id"]}) for {effective_start_time}')
        self.__ps_queue.put((effective_start_time, _id, docker_image, mission_payload, issuer_id))
        return _id

    
    def __dequeue_ps(self):
        if self.__ps_running >= self.__max_concurrent_processes:
            return
        try:
            if not self.__ps_queue.empty():
                start_time, _, _, _, _ = self.__ps_queue.queue[0]
                if time.time() < start_time:
                    return
            start_time, _id, docker_img, mission_payload, issuer_id = self.__ps_queue.get_nowait()                
            self.__logger.info(f'Dequeued new process (docker_img: {docker_img}, mission: {mission_payload["operation_id"]})')
            self.__start_new_process(docker_img, mission_payload, _id, issuer_id)
        except Empty as _:
            pass
        except Exception as e:
            self.__logger.info(e)

    def monitor(self):

        while True:
            ps = self.__active_ps.items()
            ps_running = 0
            to_delete = []
            for pid, p in ps:
                if p.get_status() == Process.TERMINATED:
                    self.__old_ps[pid] = p
                    to_delete.append(pid)
                elif p.get_status() == Process.ERROR:
                    self.__old_ps[pid] = p
                    self.__logger.info(f'\tError: {p.get_error()}')
                    to_delete.append(pid)
                elif p.get_status() == Process.HALTED:
                    self.__logger.info(f'Process {pid} has been halted.')
                    to_delete.append(pid)
                    self.__old_ps[pid] = p
                elif p.get_status() == Process.RUNNING:
                    ps_running += 1
            for d in to_delete:
                del self.__active_ps[d]
            self.__ps_running = ps_running
            self.__dequeue_ps()

            if self.__ps_queue.empty():
                time.sleep(1)

    def __get_active_processes(self):
        items = self.__active_ps.items()
        return { pid:Process.status_to_string(p.get_status()) for pid, p in self.__active_ps.items() } if items != {} else None

    def __get_old_processes(self):
        items = self.__old_ps.items()
        return { pid:Process.status_to_string(p.get_status()) for pid, p in self.__old_ps.items() } if items != {} else None

    def get_process_queue_for_user(self, user_id):
        return [e for e in list(self.__ps_queue.queue) if e[-1] == user_id]

    def processes_info(self):
        ps = {
            'active':self.__get_active_processes(),
            'old':self.__get_old_processes()
        }
        return ps