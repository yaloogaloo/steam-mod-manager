"""Local native stack capture for a Qt diagnostic callback, without changing lifecycle."""
import ctypes as c
from ctypes import wintypes as w


class Symbol(c.Structure):
    _fields_=[('SizeOfStruct',w.ULONG),('TypeIndex',w.ULONG),('Reserved',c.c_ulonglong*2),
              ('Index',w.ULONG),('Size',w.ULONG),('ModBase',c.c_ulonglong),('Flags',w.ULONG),
              ('Value',c.c_ulonglong),('Address',c.c_ulonglong),('Register',w.ULONG),('Scope',w.ULONG),
              ('Tag',w.ULONG),('NameLen',w.ULONG),('MaxNameLen',w.ULONG),('Name',c.c_char*1)]


class NativeTrace:
    def __init__(self):
        self.kernel=c.WinDLL('kernel32',use_last_error=True);self.debug=c.WinDLL('dbghelp',use_last_error=True)
        self.process=c.c_void_p(-1)
        self.debug.SymSetOptions(0x2|0x4|0x200|0x80000|0x1000)
        self.debug.SymInitializeW.argtypes=[w.HANDLE,w.LPCWSTR,w.BOOL]
        self.debug.SymInitializeW(self.process,'',True)
        self.debug.SymFromAddr.argtypes=[w.HANDLE,c.c_ulonglong,c.POINTER(c.c_ulonglong),c.POINTER(Symbol)]
        self.kernel.RtlCaptureStackBackTrace.argtypes=[w.ULONG,w.ULONG,c.POINTER(c.c_void_p),c.c_void_p]
        self.kernel.GetModuleHandleExW.argtypes=[w.DWORD,c.c_void_p,c.POINTER(w.HMODULE)]
        self.kernel.GetModuleFileNameW.argtypes=[w.HMODULE,w.LPWSTR,w.DWORD]

    def capture(self):
        pointers=(c.c_void_p*62)();count=self.kernel.RtlCaptureStackBackTrace(0,len(pointers),pointers,None)
        frames=[]
        for address in list(pointers)[:count]:
            storage=c.create_string_buffer(c.sizeof(Symbol)+1024);symbol=c.cast(storage,c.POINTER(Symbol))
            symbol.contents.SizeOfStruct=c.sizeof(Symbol);symbol.contents.MaxNameLen=1023
            displacement=c.c_ulonglong();module=w.HMODULE();name=c.create_unicode_buffer(32768)
            self.kernel.GetModuleHandleExW(0x6,c.c_void_p(address),c.byref(module))
            self.kernel.GetModuleFileNameW(module,name,len(name))
            resolved=self.debug.SymFromAddr(self.process,address,c.byref(displacement),symbol)
            function=c.string_at(c.addressof(storage)+Symbol.Name.offset,symbol.contents.NameLen).decode('utf8','replace') if resolved else '?'
            frames.append({'address':hex(address),'module':name.value,'symbol':function,'offset':hex(displacement.value)})
        return frames
