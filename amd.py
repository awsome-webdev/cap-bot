import pyopencl as cl
import numpy as np
import time

# =====================================================================
# 1. The OpenCL C Kernel (From Previous Step)
# =====================================================================
KERNEL_CODE = """
#define NUM_LIMBS 64

typedef struct { uint limbs[NUM_LIMBS]; } uint2048_t;
typedef struct { uint limbs[NUM_LIMBS * 2]; } uint4096_t;

uint2048_t sub2048(uint2048_t a, uint2048_t b, uint *borrow_out) {
    uint2048_t res;
    ulong borrow = 0;
    #pragma unroll
    for (int i = 0; i < NUM_LIMBS; i++) {
        ulong diff = (ulong)a.limbs[i] - (ulong)b.limbs[i] - borrow;
        res.limbs[i] = (uint)(diff & 0xFFFFFFFF);
        borrow = (diff >> 32) & 1;
    }
    *borrow_out = (uint)borrow;
    return res;
}

uint4096_t mul2048(uint2048_t a, uint2048_t b) {
    uint4096_t res;
    #pragma unroll
    for(int i = 0; i < NUM_LIMBS * 2; i++) { res.limbs[i] = 0; }
    
    #pragma unroll
    for (int i = 0; i < NUM_LIMBS; i++) {
        ulong carry = 0;
        #pragma unroll
        for (int j = 0; j < NUM_LIMBS; j++) {
            ulong prod = (ulong)a.limbs[i] * (ulong)b.limbs[j] + (ulong)res.limbs[i+j] + carry;
            res.limbs[i+j] = (uint)(prod & 0xFFFFFFFF);
            carry = prod >> 32;
        }
        res.limbs[i + NUM_LIMBS] = (uint)carry;
    }
    return res;
}

uint2048_t monty_reduce(uint4096_t T, uint2048_t n, uint n_prime) {
    #pragma unroll
    for (int i = 0; i < NUM_LIMBS; i++) {
        uint m = T.limbs[i] * n_prime;
        ulong carry = 0;
        #pragma unroll
        for (int j = 0; j < NUM_LIMBS; j++) {
            ulong prod = (ulong)m * (ulong)n.limbs[j] + (ulong)T.limbs[i+j] + carry;
            T.limbs[i+j] = (uint)(prod & 0xFFFFFFFF);
            carry = prod >> 32;
        }
        #pragma unroll
        for (int j = NUM_LIMBS; j < NUM_LIMBS * 2 - i; j++) {
            ulong sum = (ulong)T.limbs[i+j] + carry;
            T.limbs[i+j] = (uint)(sum & 0xFFFFFFFF);
            carry = sum >> 32;
        }
    }
    
    uint2048_t res;
    #pragma unroll
    for(int i = 0; i < NUM_LIMBS; i++) {
        res.limbs[i] = T.limbs[i + NUM_LIMBS];
    }
    
    uint borrow = 0;
    uint2048_t res_sub = sub2048(res, n, &borrow);
    
    if (borrow == 0) return res_sub;
    return res;
}

__kernel void rsw_batch_solver(
    __global const uint *x_inputs, 
    __global const uint *n_inputs, 
    __global const uint *r2_inputs,
    __global uint *results,
    int t_steps, 
    uint n_prime) 
{
    int id = get_global_id(0);
    
    uint2048_t my_x, my_n, r2_mod_n;
    #pragma unroll
    for(int i = 0; i < NUM_LIMBS; i++) {
        int offset = (id * NUM_LIMBS) + i;
        my_x.limbs[i] = x_inputs[offset];
        my_n.limbs[i] = n_inputs[offset];
        r2_mod_n.limbs[i] = r2_inputs[offset];
    }
    
    uint4096_t temp = mul2048(my_x, r2_mod_n);
    my_x = monty_reduce(temp, my_n, n_prime);
    
    for (int step = 0; step < t_steps; step++) {
        temp = mul2048(my_x, my_x);
        my_x = monty_reduce(temp, my_n, n_prime);
    }
    
    uint2048_t one;
    one.limbs[0] = 1;
    #pragma unroll
    for(int i = 1; i < NUM_LIMBS; i++) one.limbs[i] = 0;
    
    temp = mul2048(my_x, one);
    my_x = monty_reduce(temp, my_n, n_prime);
    
    #pragma unroll
    for(int i = 0; i < NUM_LIMBS; i++) {
        results[(id * NUM_LIMBS) + i] = my_x.limbs[i];
    }
}
"""

# =====================================================================
# 2. Mathematical Helpers
# =====================================================================
def int_to_limbs(val: int, num_limbs: int = 64) -> list:
    """Slices a massive Python int into 32-bit chunks (little-endian)."""
    limbs = []
    for _ in range(num_limbs):
        limbs.append(val & 0xFFFFFFFF)
        val >>= 32
    return limbs

def limbs_to_int(limbs: list) -> int:
    """Reassembles 32-bit chunks back into a massive Python int."""
    val = 0
    for i, limb in enumerate(limbs):
        val |= (int(limb) << (32 * i))
    return val

def calc_monty_constants(n: int):
    """Calculates n_prime and R^2 mod n for Montgomery reduction."""
    if n % 2 == 0:
        raise ValueError("Modulo 'n' must be an odd number for Montgomery Reduction.")
    
    # n_prime = -n^-1 mod 2^32 (Requires Python 3.8+)
    n_prime = pow(-n, -1, 2**32)
    
    # R = 2^(2048 bits) -> R^2 = 2^4096. We need R^2 mod n.
    r2_mod_n = pow(2, 4096, n)
    
    return n_prime, r2_mod_n

# =====================================================================
# 3. GPU Executor Engine
# =====================================================================
def solve_rsw_batch_gpu(x_list: list, n: int, t_steps: int):
    batch_size = len(x_list)
    print(f"Preparing batch of {batch_size} puzzles for GPU...")

    # 1. Precompute Montgomery Constants
    n_prime, r2_mod_n = calc_monty_constants(n)
    
    # 2. Pack memory into flat 32-bit arrays
    n_limbs = int_to_limbs(n, 64)
    r2_limbs = int_to_limbs(r2_mod_n, 64)
    
    # Flatten the inputs. For batching, 'n' and 'r2' are identical for all threads.
    x_flat = np.array([limb for x in x_list for limb in int_to_limbs(x, 64)], dtype=np.uint32)
    n_flat = np.array(n_limbs * batch_size, dtype=np.uint32)
    r2_flat = np.array(r2_limbs * batch_size, dtype=np.uint32)
    res_flat = np.zeros_like(x_flat)

    # 3. Initialize PyOpenCL (Auto-selects the first available GPU)
    platform = cl.get_platforms()[0]
    device = platform.get_devices(device_type=cl.device_type.GPU)[0]
    print(f"Connected to GPU: {device.name}")
    
    ctx = cl.Context([device])
    queue = cl.CommandQueue(ctx)
    
    # 4. Allocate GPU VRAM buffers
    mf = cl.mem_flags
    x_buf  = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=x_flat)
    n_buf  = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=n_flat)
    r2_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=r2_flat)
    res_buf = cl.Buffer(ctx, mf.WRITE_ONLY, res_flat.nbytes)
    
    # 5. Compile the C code
    print("Compiling OpenCL kernel (this takes a few seconds on first run)...")
    prg = cl.Program(ctx, KERNEL_CODE).build()
    
    # 6. Fire the kernel
    print(f"Launching {batch_size} parallel threads with {t_steps} steps...")
    start_time = time.time()
    
    prg.rsw_batch_solver(
        queue, 
        (batch_size,),       # Global workspace size
        None,                # Local workspace size (auto)
        x_buf, n_buf, r2_buf, res_buf, 
        np.int32(t_steps), 
        np.uint32(n_prime)
    )
    
    # Wait for the GPU to finish execution
    queue.finish()
    
    gpu_time = time.time() - start_time
    print(f"GPU Execution complete in {gpu_time:.4f} seconds!")
    
    # 7. Pull results back to CPU Ram
    cl.enqueue_copy(queue, res_flat, res_buf).wait()
    
    # 8. Reassemble the binary limbs back into Python integers
    final_results = []
    for i in range(batch_size):
        chunk = res_flat[i * 64 : (i + 1) * 64]
        final_results.append(limbs_to_int(chunk))
        
    return final_results

# =====================================================================
# 4. Main Test Script
# =====================================================================
if __name__ == "__main__":
    # Generate some massive dummy data
    print("Generating test data...")
    
    # A 2048-bit modulus 'n' (must be odd for Montgomery)
    base_n = int("198549667085808621448924376131986051958339647471595926375182243227360006893347823586234366160760038346984032048172716609025594185619154440621524335185653109365711836462007881600695514465296485059309524511632777619335257679884196581790584296266089961854532407907226985850684403225984703329116238823579941245496486348815489490076165322066240523250276589037162418980111686567244568655020195759677815028483791241393891792241032609653443115629302487246474397969922394968101201292105862285743613553804831648229528635267728972504898248001701914731461429981476848047551000363277496287795140199898679853485499997194776156015")
    
    # Number of steps per puzzle
    t_val = 300000
    
    # Number of puzzles to solve simultaneously (Keep this low for testing, e.g., 1000. Increase for production)
    batch_size = 2
    
    # Generate 1000 slightly different starting 'x' values
    x_batch = [base_n - i for i in range(1, batch_size + 1)]
    
    # Run the engine
    results = solve_rsw_batch_gpu(x_batch, base_n, t_val)
    
    print("\nFirst 3 Results:")
    for i in range(3):
        print(f"Puzzle {i}: {str(results[i])}")