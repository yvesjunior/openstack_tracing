def event(event_type,inst,cont,msg):
    if(inst is None):
    	instance=INSTANCE()
    else:
    	instance=inst
    if (cont is None):
	context=CONTEXT()
    else:
    	context=cont
    Message ="{'event_type':"+str(event_type)+\
    	",'vmname':'"+str(instance.hostname)+\
    	"','vm_state':'"+str(instance.vm_state)+\
   	"','host':'"+str(instance.host)+\
	"','project_id':'"+str(instance.project_id)+\
	"','vm_task':'"+str(instance.task_state)+\
	"','request_id':'"+str(context.request_id)+\
	"','msg':'"+str(msg)+"'}"
    return "lttng_trace:"+Message
	
def newContext():
    context=dict()
    context.request_id=''
    return context

class INSTANCE:
    def __init__(self):
        self.hostname=''
        self.host=''
        self.vm_state=''
        self.host=''
        self.project_id=''
        self.task_state=''
class CONTEXT:
    def __init__(self):
        self.request_id=''
