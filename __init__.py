import asyncio
import json
import os
import shutil
import copy
import time
from server import PromptServer
from comfy import samplers
import comfy.utils
from aiohttp import web


# Constants
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIRECTORY = "web"  # This will be the directory for web resources
DIR_WEB = os.path.join(THIS_DIR, WEB_DIRECTORY)

# Remove old directories if they exist
OLD_DIRS = [
    os.path.abspath(f'{THIS_DIR}/../../web/extensions/task_monitor'),
]
for old_dir in OLD_DIRS:
    if os.path.exists(old_dir):
        shutil.rmtree(old_dir)

# Ensure web directory exists
if not os.path.exists(DIR_WEB):
    os.makedirs(DIR_WEB)

# Add web directory to ComfyUI's static files
WEB_DIRECTORY = os.path.join(THIS_DIR, "web")

class TaskMonitorNode:
    NAME = "TaskMonitorNode"
    
    def __init__(self):
        self.current_task_id = None
        self.current_node_id = None
        self.current_node_type = None
        self.progress_value = 0
        self.progress_max = 0
        self.last_status_message = None
        self.last_progress_message = None
        self.output_cache = {}  # Cache for storing outputs once a task is done

        # New status for overall workflow progress
        self.prompt_workflow = None  # The actual workflow dictionary for the current_task_id
        self.total_nodes_in_prompt = 0
        self.executed_node_ids_set = set()  # Set of node IDs (as strings) that have started execution for current_task_id
        self.last_executed_node_id = None  # The most recent node_id from an 'executing' event (node != null)
        self.execution_list = None  # Reference to the current execution list

        self.start_time = None  # Track when execution starts
        self.total_execution_time = 0  # Total execution time in seconds
        
        # add event handlers
        self.event_handlers = {
            "status": self.on_status,
            "execution_start": self.on_execution_start,
            "executing": self.on_executing,
            "execution_cached": self.on_execution_cached,
            "progress": self.on_progress,
            "executed": self.on_executed,
            "execution_error": self.on_execution_error
        }

    def reset_workflow_progress(self):
        self.prompt_workflow = None
        self.total_nodes_in_prompt = 0
        self.executed_node_ids_set = set()  # This will store string node IDs
        self.last_executed_node_id = None
        self.execution_list = None
        self.progress_value = 0
        self.progress_max = 0
        self.start_time = None
        self.total_execution_time = 0

    def set_current_task(self, prompt_id, workflow_dict):
        if self.current_task_id != prompt_id:
            self.reset_workflow_progress()
            self.current_task_id = prompt_id
            self.prompt_workflow = workflow_dict
            if self.prompt_workflow:
                # Count only nodes that are not input/output nodes and are actually needed for execution
                self.total_nodes_in_prompt = len([
                    key for key in self.prompt_workflow.keys() 
                    if key.isdigit() and 
                    self.prompt_workflow[key].get("class_type") not in ["TaskMonitorNode"]
                ])
        elif not self.prompt_workflow and workflow_dict:
            self.prompt_workflow = workflow_dict
            if self.prompt_workflow:
                # Count only nodes that are not input/output nodes and are actually needed for execution
                self.total_nodes_in_prompt = len([
                    key for key in self.prompt_workflow.keys() 
                    if key.isdigit() and 
                    self.prompt_workflow[key].get("class_type") not in ["TaskMonitorNode"]
                ])
    
    def on_status(self, data):
        if not data.get("status"):
            return
            
        queue_info = data.get("status", {})
        exec_info = queue_info.get("exec_info", {})
        queue_remaining = exec_info.get("queue_remaining", 0)
        
        # get current task
        server = PromptServer.instance
        running_tasks, pending_tasks = server.prompt_queue.get_current_queue()
        if running_tasks:
            current_task = running_tasks[0]
            if len(current_task) > 2:
                prompt_id = current_task[1]
                workflow = current_task[2]
                if prompt_id != self.current_task_id:
                    self.reset_workflow_progress()
                    self.current_task_id = prompt_id
                    self.prompt_workflow = workflow
                    # count only nodes that are not input/output nodes and are actually needed for execution
                    self.total_nodes_in_prompt = len([
                        key for key in workflow.keys() 
                        if key.isdigit() and 
                        workflow[key].get("class_type") not in ["TaskMonitorNode"]
                    ])
    
    def on_execution_start(self, data):
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return
            
        # get current task
        server = PromptServer.instance
        running_tasks, _ = server.prompt_queue.get_current_queue()
        for task in running_tasks:
            if len(task) > 2 and task[1] == prompt_id:
                workflow = task[2]
                self.set_current_task(prompt_id, workflow)
                # record start time
                self.start_time = time.time()
                self.total_execution_time = 0
                break
    
    def on_executing(self, data):
        node_id = data.get("node")
        prompt_id = data.get("prompt_id")
        
        if prompt_id == self.current_task_id:
            if node_id is not None:
                # convert node ID to string to maintain consistency
                str_node_id = str(node_id)
                # count only nodes that are not input/output nodes and are actually needed for execution
                if (self.prompt_workflow and 
                    str_node_id in self.prompt_workflow and 
                    self.prompt_workflow[str_node_id].get("class_type") not in ["TaskMonitorNode"]):
                    
                    self.current_node_id = node_id
                    self.current_node_type = self.prompt_workflow[str_node_id].get("class_type")
                    self.last_executed_node_id = node_id
                    
                    if str_node_id not in self.executed_node_ids_set:
                        self.executed_node_ids_set.add(str_node_id)
            else:
                # node is None means execution is completed
                # use actual node IDs instead of assuming they are sequential integers
                if self.prompt_workflow and self.total_nodes_in_prompt > 0:
                    # get all valid node IDs from the workflow
                    valid_node_ids = [
                        key for key in self.prompt_workflow.keys() 
                        if key.isdigit() and 
                        self.prompt_workflow[key].get("class_type") not in ["TaskMonitorNode"]
                    ]
                    self.executed_node_ids_set = set(valid_node_ids)
    
    def on_execution_cached(self, data):
        prompt_id = data.get("prompt_id")
        cached_nodes = data.get("nodes", [])
        
        if prompt_id == self.current_task_id:
            for node_id in cached_nodes:
                str_node_id = str(node_id)
                if (self.prompt_workflow and 
                    str_node_id in self.prompt_workflow and 
                    self.prompt_workflow[str_node_id].get("class_type") not in ["TaskMonitorNode"]):
                    
                    if str_node_id not in self.executed_node_ids_set:
                        self.executed_node_ids_set.add(str_node_id)
    
    def on_progress(self, data):
        self.progress_value = data.get("value", 0)
        self.progress_max = data.get("max", 0)
        
        # update current node information
        node_id = data.get("node")
        if node_id and self.prompt_workflow:
            str_node_id = str(node_id)
            node_info = self.prompt_workflow.get(str_node_id)
            if node_info:
                self.current_node_type = node_info.get("class_type")
                self.current_node_id = node_id
    
    def on_executed(self, data):
        # calculate total execution time
        if self.start_time is not None:
            self.total_execution_time = time.time() - self.start_time
            self.start_time = None
    
    def on_execution_error(self, data):
        # task execution error
        self.last_status_message = f"Error: {data.get('exception_message', 'Unknown error')}"
    
    def handle_event(self, event_type, data):
        handler = self.event_handlers.get(event_type)
        if handler:
            try:
                handler(data)
            except Exception as e:
                print(f"[TaskMonitor] Error handling event {event_type}: {e}")

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ()
    FUNCTION = "do_nothing"
    OUTPUT_NODE = True
    CATEGORY = "utils"

    def do_nothing(self, prompt=None, extra_pnginfo=None):
        return ()

# Create the global instance
TaskMonitorNode.instance = TaskMonitorNode()

# Server utilities
def get_param(request, param, default=None):
    """Get a parameter from a request query string."""
    return request.rel_url.query[param] if param in request.rel_url.query else default

def is_param_truthy(request, param):
    """Check if a parameter is explicitly true."""
    val = get_param(request, param)
    return val is not None and val.lower() not in ("0", "false", "no")

# Route handlers
async def get_task_status(request):
    """Handle GET requests to /task_monitor/status."""
    monitor_node = TaskMonitorNode.instance
    server = PromptServer.instance
    queue = server.prompt_queue
    
    running_tasks, pending_tasks_queue = queue.get_current_queue()
    current_processing_prompt_id = getattr(server, 'last_prompt_id', None)
    
    # Get the actual execution list if available
    total_nodes = monitor_node.total_nodes_in_prompt
    executed_nodes = len(monitor_node.executed_node_ids_set)

    # Calculate current execution time if task is running
    current_execution_time = monitor_node.total_execution_time
    if monitor_node.start_time is not None:
        current_execution_time = time.time() - monitor_node.start_time
    
    # If we don't have the total nodes count yet, try to get it from the current task
    if total_nodes == 0 and running_tasks:
        for task in running_tasks:
            if len(task) > 2:
                workflow = task[2]
                if workflow:
                    total_nodes = len([
                        key for key in workflow.keys() 
                        if key.isdigit() and 
                        workflow[key].get("class_type") not in ["TaskMonitorNode"]
                    ])
                    monitor_node.total_nodes_in_prompt = total_nodes
                    monitor_node.prompt_workflow = workflow
                break
    
    status_data = {
        "task_id": current_processing_prompt_id,
        "status": "idle",
        "queue": {
            "running_count": len(running_tasks),
            "pending_count": len(pending_tasks_queue),
            "running": [],
            "pending": []
        },
        "current_task_progress": None,
        "current_task_outputs": None,
        "error_info": None,
        "workflow_progress": {
            "total_nodes": total_nodes,
            "executed_nodes": executed_nodes,
            "last_executed_node_id": monitor_node.last_executed_node_id
        },
        "execution_time": current_execution_time,# Add execution time in seconds
    }

    # Populate queue information
    for task_list, status in [(running_tasks, "running"), (pending_tasks_queue, "pending")]:
        for task_item in task_list:
            prompt_id = task_item[1]
            status_data["queue"][status].append({
                "prompt_id": prompt_id,
                "nodes_in_prompt": len(task_item[2]) if len(task_item) > 2 else 0,
                "client_id": task_item[4].get('client_id') if len(task_item) > 4 and isinstance(task_item[4], dict) else None
            })

    if not current_processing_prompt_id and running_tasks:
        current_processing_prompt_id = running_tasks[0][1]
        status_data["task_id"] = current_processing_prompt_id

    if current_processing_prompt_id:
        history = queue.get_history(prompt_id=current_processing_prompt_id)
        if current_processing_prompt_id in history:
            task_history = history[current_processing_prompt_id]
            if task_history.get('status', {}).get('status_str') == 'success':
                status_data["status"] = "completed"
                status_data["current_task_outputs"] = {
                    "prompt_id": current_processing_prompt_id,
                    "outputs": task_history.get("outputs", {})
                }
            elif task_history.get('status', {}).get('status_str') == 'error':
                status_data["status"] = "error"
                status_data["error_info"] = task_history.get('status', {}).get('messages', [])
            if current_processing_prompt_id in monitor_node.output_cache:
                del monitor_node.output_cache[current_processing_prompt_id]
        else:
            is_running = any(task[1] == current_processing_prompt_id for task in running_tasks)
            if is_running:
                status_data["status"] = "running"
                status_data["current_task_progress"] = {
                    "node_id": monitor_node.current_node_id,
                    "node_type": monitor_node.current_node_type,
                    "step": monitor_node.progress_value,
                    "total_steps": monitor_node.progress_max,
                    "text_message": monitor_node.last_progress_message
                }
            else:
                is_queued = any(task[1] == current_processing_prompt_id for task in pending_tasks_queue)
                if is_queued:
                    status_data["status"] = "queued"
                else:
                    # Not in history, not running, not queued - must be idle or error
                    status_data["status"] = "idle"

    return web.json_response(status_data)

# global hooks
_original_progress_hook = None
_original_send_sync_method = None

# progress hook function - only used to get progress information, does not affect the main interface
def task_monitor_progress_hook(value, total, preview_image):
    """Hook for progress updates."""
    monitor = TaskMonitorNode.instance
    server = PromptServer.instance
    
    # save progress information
    monitor.progress_value = value
    monitor.progress_max = total
    
    # update current node information
    current_node_id = getattr(server, 'last_node_id', None)
    if current_node_id and monitor.prompt_workflow:
        str_node_id = str(current_node_id)
        if str_node_id in monitor.prompt_workflow:
            monitor.current_node_id = current_node_id
            monitor.current_node_type = monitor.prompt_workflow[str_node_id].get("class_type")
    
    # always call the original hook to ensure it doesn't affect the main interface
    if _original_progress_hook:
        try:
            return _original_progress_hook(value, total, preview_image)
        except Exception as e:
            print(f"[TaskMonitor] Error in original_progress_hook: {e}")
    
    return preview_image

# server event interceptor function
def task_monitor_send_sync(event, data, sid=None):
    # first handle the event
    monitor = TaskMonitorNode.instance
    monitor.handle_event(event, data)
    
    # then call the original method
    if _original_send_sync_method:
        return _original_send_sync_method(event, data, sid)
    return None

def setup_hooks():
    global _original_progress_hook, _original_send_sync_method
    
    # save original hook and set our hook
    if comfy.utils.PROGRESS_BAR_HOOK and comfy.utils.PROGRESS_BAR_HOOK != task_monitor_progress_hook:
        _original_progress_hook = comfy.utils.PROGRESS_BAR_HOOK
    
    # set progress hook
    comfy.utils.set_progress_bar_global_hook(task_monitor_progress_hook)
    
    # set server event hook
    server = PromptServer.instance
    if hasattr(server, 'send_sync') and not hasattr(server, '_original_send_sync_tm_bound'):
        _original_send_sync_method = server.send_sync
        server._original_send_sync_tm_bound = server.send_sync
        
        # use function replacement
        def new_send_sync(event, data, sid=None):
            # first handle the event
            monitor = TaskMonitorNode.instance
            try:
                monitor.handle_event(event, data)
            except Exception as e:
                print(f"[TaskMonitor] Error handling event {event}: {e}")
            
            # then call the original method
            return _original_send_sync_method(event, data, sid)
        
        server.send_sync = new_send_sync

def register_routes():
    if hasattr(PromptServer.instance, "app") and PromptServer.instance.app is not None:
        # Add static file serving
        PromptServer.instance.app.router.add_static('/task_monitor', DIR_WEB)
        
        # Add API routes
        PromptServer.instance.app.add_routes([
            web.get('/task_monitor/status', get_task_status)
        ])
    else:
        async def deferred_register():
            await asyncio.sleep(1)
            if hasattr(PromptServer.instance, "app") and PromptServer.instance.app is not None:
                # Add static file serving
                PromptServer.instance.app.router.add_static('/task_monitor', DIR_WEB)
                
                # Add API routes
                PromptServer.instance.app.add_routes([
                    web.get('/task_monitor/status', get_task_status)
                ])
        
        if hasattr(PromptServer.instance, "loop") and PromptServer.instance.loop is not None:
            PromptServer.instance.loop.create_task(deferred_register())
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(deferred_register())
            except RuntimeError:
                pass

# delay setup hooks and routes
async def deferred_setup():
    await asyncio.sleep(1.0)
    if PromptServer.instance:
        # set hooks
        setup_hooks()
        print("[TaskMonitor] Task progress monitoring started")

# delay register routes
if PromptServer.instance:
    register_routes()
else:
    register_routes()

try:
    loop = asyncio.get_running_loop()
    loop.create_task(deferred_setup())
except RuntimeError:
    if PromptServer.instance and hasattr(PromptServer.instance, "loop"):
        PromptServer.instance.loop.create_task(deferred_setup())

# Node registration
NODE_CLASS_MAPPINGS = {
    TaskMonitorNode.NAME: TaskMonitorNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    TaskMonitorNode.NAME: "Task Monitor API Node"
}

# Export for ComfyUI
__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']
