"""Start the kernel: python -m c_interactive_kernel -f connection_file (Jupyter and VS Code do this)"""

from ipykernel.kernelapp import IPKernelApp

from c_interactive_kernel.kernel import CKernel

IPKernelApp.launch_instance(kernel_class=CKernel)
