# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import (
    absolute_import,
    annotations,
    division,
    print_function,
    unicode_literals,
)

import logging
import os
import random
import sys
import time
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from collections import OrderedDict
from contextlib import ContextDecorator
from dataclasses import dataclass
from io import StringIO
from typing import Any, Callable, Dict, List, Optional, Union

import torch
from torch._C._distributed_c10d import ProcessGroup
from torch.autograd.profiler import record_function

random.seed()

logger = logging.getLogger(__name__)

default_master_ip = "127.0.0.1"
default_master_port = "29500"


def gracefulExit(args: Any = 0) -> None:
    """
    Use this function to gracefully exit if any fatal errors are encountered.

    Args:
        args: Message you want to print out.
    Returns:
        None: Will cause program to terminate.
    """
    # TODO: Is this the best way to exit?
    if args != 0:
        logger.error(args)
    # WARNING: Assuming sys is always used, should find a platform-independent way to gracefully exit.
    sys.exit(args)


def parsesize(ipValue: str) -> int:
    """
    nccl-tests compatible input-size parsing.

    Args:
        ipValue: Contains size of input.
    Returns:
        size: Returns the size of input.
    """
    units = 0
    size = 0.0

    value = ""

    # This function would be invoked in a loop - once for each data-type. For  first iteration, ipValue is of type string but after that,
    # the type of ipValue equals the returntype of prior iteration ie; int. Hence, type check is moved up as first condition.
    if isinstance(ipValue, int) or ipValue.isnumeric():
        units = 1
        value = ipValue

    elif ipValue.find("G") != -1:
        units = 1024 * 1024 * 1024
        unitIdx = ipValue.find("G")
        value = ipValue[0:unitIdx]

    elif ipValue.find("M") != -1:
        units = 1024 * 1024
        unitIdx = ipValue.find("M")
        value = ipValue[0:unitIdx]

    elif ipValue.find("K") != -1:
        units = 1024
        unitIdx = ipValue.find("K")
        value = ipValue[0:unitIdx]

    else:
        logger.error(f"Could not parse input size {ipValue}")
        gracefulExit()

    size = int(value) * units
    return int(size)


def parseRankList(
    ipStr: str, ipName: str, comms_world_info: comms_world_info_holder
) -> List[int]:
    """
    Parses a string into a rank list.

    Args:
        ipStr: String containing list of ranks or single rank.
        ipName: Name of describing ranks, src_ranks, dst_ranks, etc
        comms_world_info: Class containing world information.
    Returns:
        List: Returns list containing the ranks from ipStr
    """
    rankList = []  # default empty

    if ipStr:
        if ipStr.isnumeric():
            # single rank
            rankList = [int(ipStr)]
        elif ipStr.find(",") != -1:
            # list of unique ranks separated by comma
            rankList = list(map(int, [r.strip() for r in ipStr.split(",")]))
            rankList = list(OrderedDict.fromkeys(rankList))
        elif ipStr.find(":") != -1:
            # a range of ranks defined by [start:end]
            pos = list(map(int, [r.strip() for r in ipStr.split(":")]))
            rankList = [*range(pos[0], pos[1] + 1)]

        # Check if input is valid
        if len(rankList) == 0 or any(
            r < 0 or r >= comms_world_info.world_size for r in rankList
        ):
            if comms_world_info.global_rank == 0:
                logger.error(f"Could not parse {ipName}: {ipStr}")
            gracefulExit()
    return rankList


def getAlgBW(elapsedTimeNS: float, dataSize: int, numIters: int) -> (float, float):
    """
    Similar to how algorithmic bandwidth is computed in nccl-tests.

    Args:
        elapsedTimeNS: Total elapsed time for run in ns.
        dataSize: Size in bytes of the data being ran.
        numIters: Number of iterations for run.
    Returns:
        (avgIterNs, algBW): Returns the average amount of time in ns per iteration, and the algBW (GBps) calculated.
    """
    avgIterNS = 0.0
    if numIters != 0:
        avgIterNS = elapsedTimeNS / numIters

    algBW = 0.0
    if avgIterNS != 0:
        algBW = (dataSize) / (avgIterNS)  # dataSize dividied by ns gives us GBps
    return (avgIterNS, algBW)


def getSizes(
    beginSize: int, endSize: int, stepFactor: int, stepBytes: int
) -> List[int]:
    """
    Gets the sizes of each iteration.

    Args:
        beginSize: Size of first iteration.
        endSize: Size of last iteration.
        stepFactor: Factor that each iteration increases by.
    Returns:
        allSizes: List that contains size of each iteration up to endSize.
    """
    curSize = beginSize
    numIters = 0
    maxIters = 100
    allSizes = []
    while curSize <= endSize:
        allSizes.append(curSize)
        curSize = curSize * stepFactor if stepBytes == 0 else curSize + stepBytes
        numIters = numIters + 1
        if numIters > 100:
            logger.error(
                f"For finding allSizes numIters: {numIters} is greater than maxIters: {maxIters}"
            )
            break
    return allSizes


def fixBeginSize(commsParams: commsParamsHolder, world_size: int) -> None:
    """
    Validate begin size to match other parameters.

    Args:
        commsParams: Holds beginSize and other parameters to perform validation.
        world_size: The total number of global ranks.
    Returns:
        None
    """
    # ensures we will have atleast one member/rank
    if commsParams.collective in (
        "all_to_all",
        "all_to_allv",
        "all_gather",
        "all_gather_base",
        "gather",
        "reduce_scatter_base",
    ):
        if (commsParams.beginSize / commsParams.element_size) < world_size:
            commsParams.beginSize = world_size * commsParams.element_size

        if (
            commsParams.bitwidth < 32
            and (commsParams.beginSize / commsParams.element_size / world_size)
            < commsParams.quant_a2a_embedding_dim
        ):
            commsParams.beginSize = (
                commsParams.quant_a2a_embedding_dim
                * world_size
                * commsParams.element_size
            )
    elif (commsParams.collective == "all_reduce") or (
        commsParams.collective == "reduce"
    ):
        if commsParams.beginSize < commsParams.element_size:
            commsParams.beginSize = commsParams.element_size


def get_rank_details(
    backendFuncs: backendFunctions,
) -> (int, int, int, ProcessGroup, str, str):
    """
    Returns the details of the rank for the current backendFunction.

    Args:
        backendFuncs: Backend we are gathering information from.
    Returns:
        (local_rank, global_rank, world_size, group, curDevice, curHwDevice): Returns the values of these in the provided backendFunction.
    """
    local_rank = backendFuncs.get_local_rank()
    global_rank = backendFuncs.get_global_rank()
    world_size = backendFuncs.get_world_size()
    group = backendFuncs.get_default_group()
    curDevice = backendFuncs.get_device()
    curHwDevice = backendFuncs.get_hw_device()

    return (local_rank, global_rank, world_size, group, curDevice, curHwDevice)


def env2int(env_list: List[str], default: int = -1) -> int:
    """
    Takes environment variables list and returns the first value found.

    Args:
        env_list: List of environment variables.
        default: Default value to return if all environment variables are not set.
    Returns:
        val: Returns value located at one of the environment variables, or returns default value if none are set.
    """
    for e in env_list:
        val = int(os.environ.get(e, -1))
        if val >= 0:
            return val
    return default


def read_comms_env_vars() -> Dict[str, int]:
    """
    Reads environment variables and record them.

    Args:
        None
    Returns:
        comms_env_params: Dict containing env var name as key and int for that env var as value.
    """
    world_size = env2int(
        ["MV2_COMM_WORLD_SIZE", "OMPI_COMM_WORLD_SIZE", "PMI_SIZE", "WORLD_SIZE"], -1
    )

    local_size = env2int(
        [
            "LOCAL_SIZE",
            "MPI_LOCALNRANKS",
            "MV2_COMM_WORLD_LOCAL_SIZE",
            "OMPI_COMM_WORLD_LOCAL_SIZE",
        ],
        -1,
    )

    global_rank = env2int(
        ["MV2_COMM_WORLD_RANK", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "RANK"], -1
    )

    local_rank = env2int(
        [
            "LOCAL_RANK",
            "MPI_LOCALRANKID",
            "MV2_COMM_WORLD_LOCAL_RANK",
            "OMPI_COMM_WORLD_LOCAL_RANK",
        ],
        -1,
    )

    comms_env_params = {}
    comms_env_params["world_size"] = world_size
    comms_env_params["local_size"] = local_size
    comms_env_params["global_rank"] = global_rank
    comms_env_params["local_rank"] = local_rank
    return comms_env_params


def commonUrlRead(remotePath: str) -> StringIO:
    """
    Reads content at remotePath.

    Args:
        remotePath: URL of where to read from.
    Returns:
        StringIO: Return decoded StringIO for contents of url.
    """
    import urllib.request

    # TODO: Error handle
    with urllib.request.urlopen(remotePath) as rf:
        contents = rf.read()
    return StringIO(contents.decode("utf-8"))


def initQuantCommCtx(
    collectiveArgs: collectiveArgsHolder, commsParams: commsParamsHolderBase
) -> None:
    """
    Initialize quantization handlers.

    Args:
        collectiveArgs: This will be modified to support quantization.
        commsParams: Holds parameters used to setup quantization (bidwidth).
    Returns:
        None
    """
    logger.info(f"communication bitwidth set to {commsParams.bitwidth}")
    try:
        from internals import initialize_collectiveArgs_internal

        initialize_collectiveArgs_internal(collectiveArgs, commsParams)
    except ImportError:
        # cannot do quantization, reset bitwidth
        logger.warning("quantization not supported, disabled and continue...")
        commsParams.bitwidth = 32
        pass


def checkQuantArgs(
    collective: str,
    dtype: torch.dtype,
    beginSize: int,
    quant_a2a_embedding_dim: int,
    blockingFlag: bool,
) -> None:
    """
    Checks quantized args passed in parameter list to make sure they are supported, will exit if not.

    Args:
        collective: Name of collective to be quantized.
        dtype: Torch datatype of collective.
        beginSize: Starting size.
        quant_a2a_embedding_dim: Quant embedding dimension for all_to_all.
        blockingFlag: Flag to specify whether the collective will be ran in blocking or non-blocking mode.
    Returns:
        None
    """
    if collective not in (
        "all_to_all",
        "all_to_allv",
        "reduce",
        "all_reduce",
    ):
        raise NotImplementedError(
            f"quantized communication for {collective} is currently unsupported."
        )
    if collective in ("all_to_all", "all_to_allv"):
        if (beginSize // 4) % quant_a2a_embedding_dim != 0:
            logger.warning(
                f"begin size {beginSize} must be a multiple of --quant-a2a-embedding-dim {quant_a2a_embedding_dim} for all_to_all operation"
            )
        if blockingFlag != 1:
            raise NotImplementedError("quantized All_to_all must be synchronous.")
    if dtype != torch.float32:
        raise NotImplementedError(
            f"quantization for {dtype} is not supported. Use float32 instead."
        )


def clearQuantCommCtx(collectiveArgs: collectiveArgsHolder) -> None:
    """
    Cleans up quantization handlers.

    Args:
        collectiveArgs: Contains the quantization handlers.
    Returns:
        None
    """
    try:
        logger.debug("Removing installed quantization handlers.")
        from internals import remove_quantization_handlers

        remove_quantization_handlers(collectiveArgs)
    except ImportError:
        pass


def paramToCommName(name: str, supported_comms: List[str] = None) -> str:
    """
    Map any possible creative collective names to the internal name.
    Validate the `name` if `supported_comms` is provided.

    Args:
        name: Name of collective.
        supported_comms: List of supported comms to check in.
    Returns:
        new_name: Returns the formatted name if supported_comms is empty, or name is in supported_comms.
    """
    name_aliases = {
        "alltoall": "all_to_all",
        "alltoallv": "all_to_allv",
        "alltoallbase": "all_to_allv",
        "allreduce": "all_reduce",
        "allgather": "all_gather",
        "allgatherbase": "all_gather_base",
        "reducescatter": "reduce_scatter",
        "recvanysource": "recv",
    }

    new_name = name.lower()

    new_name = "".join(x for x in new_name if x.isalpha())
    if new_name in name_aliases:
        new_name = name_aliases[new_name]
    else:
        new_name = name

    if supported_comms is not None and new_name not in supported_comms:
        gracefulExit(
            f"{name} is not a supported communication in PARAM! Supported comms: {supported_comms}"
        )

    return new_name


def ensureTensorFlush(tensors: Union[List[torch.Tensor], torch.Tensor]) -> float:
    """
    Use this to flush non-blocking ops to ensure they are really complete.

    Args:
        tensors: Retrieve item of last tensor to force flush.
    Returns:
        x: A standard python number, can be float or int.
    """
    x = None
    if isinstance(tensors, list) and len(tensors) > 0 and len(tensors[-1]) > 0:
        # some collectives like allgather use a list of tensors
        x = tensors[-1][-1].item()  # to ensure collective won't be optimized away.
    elif isinstance(tensors, torch.Tensor) and tensors.nelement() > 0:
        x = tensors[-1].item()  # to ensure collective won't be optimized away.

    return x


def startProfiler(rank: int, device: str, numWarmupIters: int, numIters: int) -> bool:
    """
    Starts internal profiler with given parameters.

    Args:
        rank: Global rank.
        device: Type of device "cuda", "cpu", etc.
        numWarmupIters: Number of warmup iterations.
        numIters: Number of real iterations.
    Returns:
        bool: Returns if internal profile was able to start or not.
    """
    try:
        from internals import fbInitProfiler, fbStartProfiler

        fbInitProfiler(
            rank=rank,
            device=device,
            warmup=numWarmupIters,
            iters=numIters,
        )
        fbStartProfiler()
        return True
    except ImportError:
        logger.debug("Internal profiler is not available, skip...")
    else:
        return False


def sampleProfiler(stop: bool = False) -> None:
    """
    Starts internal sample profiler.

    Args:
        stop: Bool to be passed into sample profiler.
    Returns:
        None
    """
    try:
        from internals import fbSampleProfiler

        fbSampleProfiler(stop)
    except ImportError:
        logger.debug("Internal profiler is not available, skip...")


@dataclass
class paramTimer:
    """
    Timer for param profiler.
    """

    elapsedTimeNS: float = 0.0  # keeping time in NS

    def reset(self, newTime: float = 0.0) -> None:
        self.elapsedTimeNS = newTime

    def incrTimeNS(self, timeNS: float) -> None:
        self.elapsedTimeNS += timeNS

    def getTimeUS(self) -> float:
        return self.elapsedTimeNS / 1e3

    def getTimeNS(self) -> float:
        return self.elapsedTimeNS


class commsArgs:
    """
    This class contains all of the args that we can use to perform a single collective.

    Public Attributes:
        comms: Name of collective.
        seqnum: Current number of collectives.
        req: Request ID of collective to map to wait operation.
        inMsgSize: Size of input tensor.
        outMsgSize: Size of output tensor.
        dtype: Data type of tensor values.
        inSplit: List of input split sizes for rank across current process group.
        outSplit: List of output split sizes for ranks across current process group.
        startTimeNs: Start time of current collective.
        pgId: Unique indentifier for the process group this collective will use.
        groupRanks: Global ranks of the process group, this is used with PG init.
        worldSize: World size of current process group.
        markerStack: Current markers that this collective is a part of.
        root: Used to determine if collective is src or dst.
        eg_id: Node id in captured execution graph.
    """

    def __init__(self, **kwargs) -> None:
        """
        Initialize arguments used for comm replay.
        """
        self.comms = kwargs["comms"] if "comms" in kwargs else None
        self.seqnum = kwargs["seqnum"] if "seqnum" in kwargs else None
        self.req = kwargs["req"] if "req" in kwargs else None
        self.inMsgSize = kwargs["inMsgSize"] if "inMsgSize" in kwargs else None
        self.outMsgSize = kwargs["outMsgSize"] if "outMsgSize" in kwargs else None
        self.dtype = kwargs["dtype"] if "dtype" in kwargs else None
        self.inSplit = kwargs["inSplit"] if "inSplit" in kwargs else None
        self.outSplit = kwargs["outSplit"] if "outSplit" in kwargs else None
        self.startTimeNs = kwargs["startTimeNs"] if "startTimeNs" in kwargs else None
        self.pgId = kwargs["pgId"] if "pgId" in kwargs else None
        self.groupRanks = kwargs["groupRanks"] if "groupRanks" in kwargs else None
        self.worldSize = kwargs["worldSize"] if "worldSize" in kwargs else None
        self.markerStack = kwargs["markerStack"] if "markerStack" in kwargs else None
        self.root = kwargs["root"] if "root" in kwargs else None
        self.eg_id = kwargs["eg_id"] if "eg_id" in kwargs else None

    def toDict(self) -> Dict:
        """
        Convert commsArgs to dictionary for storing in json.

        Args:
            None
        Returns:
            commData: Dictionary containing the comms metadata.
        """
        commData = {}
        commData["comms"] = self.comms
        commData["seqnum"] = self.seqnum
        if self.req is not None:
            commData["req"] = self.req
        if self.inMsgSize is not None:
            commData["in_msg_size"] = self.inMsgSize
            commData["out_msg_size"] = self.outMsgSize
            commData["dtype"] = self.dtype
        if self.inSplit is not None:
            commData["in_split"] = self.inSplit
        if self.outSplit is not None:
            commData["out_split"] = self.outSplit
        if self.startTimeNs is not None:
            commData["startTime_ns"] = self.startTimeNs
        if self.pgId is not None:
            commData["pg_id"] = self.pgId
        if self.worldSize is not None:
            commData["world_size"] = self.worldSize
        if self.root is not None:
            commData["root"] = self.root

        return commData

    def __eq__(self, other: commsArgs) -> bool:
        """
        Used for testing. Check if two comms are equal.
        """
        return self.__dict__ == other.__dict__

    def __repr__(self):
        """
        Print repr of commsArgs in human readable format.
        """
        return self.__dict__.__str__()

    def __str__(self) -> str:
        """
        Print out the commsArgs in human readable format.
        """
        return self.__dict__.__str__()


class paramProfile(record_function):
    """Inherit from PyTorch profiler to enable autoguard profiling while measuring the time interval in PARAM"""

    def __init__(self, timer: paramTimer = None, description: str = "") -> None:
        self.description = description
        self.timer = timer
        super().__init__(name=description)

    def __enter__(self) -> paramProfile:
        super().__enter__()
        self.start = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.end = time.monotonic()
        self.intervalNS = (self.end - self.start) * 1e9  # keeping time in NS
        # if given a valid paramTimer object, directly update the measured time interval
        if isinstance(self.timer, paramTimer):
            self.timer.incrTimeNS(self.intervalNS)
        logger.debug(f"{self.description} took {self.intervalNS} ns")
        super().__exit__(exc_type, exc_value, traceback)


class paramStreamGuard(ContextDecorator):
    """guard execution on a stream"""

    def __init__(
        self,
        stream: Optional[torch.cuda.Stream],
        curDevice: torch.device,
        backendFuncs: backendFunctions,
    ) -> None:
        self.cur_stream = None
        self.stream = stream
        self.curDevice = curDevice
        self.backendFuncs = backendFuncs

    def __enter__(self) -> paramStreamGuard:
        self.cur_stream = self.backendFuncs.switch_stream(self.stream, self.curDevice)
        return self

    def __exit__(self, *exc) -> None:
        self.backendFuncs.switch_stream(self.cur_stream, self.curDevice)


class backendFunctions(ABC):
    """Abstract base class, provides common abstraction for all the backends."""

    def __init__(self) -> None:
        self.tcp_store = None
        self.collectiveFunc = {
            "all_to_all": self.all_to_all,
            "all_to_allv": self.all_to_allv,
            "all_reduce": self.all_reduce,
            "broadcast": self.broadcast,
            "gather": self.gather,
            "all_gather": self.all_gather,
            "all_gather_base": self.all_gather_base,
            "reduce": self.reduce,
            "reduce_scatter": self.reduce_scatter,
            "reduce_scatter_base": self.reduce_scatter_base,
            "scatter": self.scatter,
            "barrier": self.barrier,
            "incast": self.incast,
            "multicast": self.multicast,
            "noop": self.noop,
        }

    def getBusBW(
        self, collective: str, algBW: float, collectiveArgs: collectiveArgsHolder
    ) -> float:
        """
        Calculate bus bandwidth for collective.

        Args:
            collective: Name of collective.
            algBW: Algorithmic bandwidth for the collective.
            collectiveArgs: Contains information about world size.
        Returns:
            busBW: Bus bandwidth in GBps
        """
        busBW = algBW
        mulFactor = 1.0
        if collective == "all_reduce":
            if collectiveArgs.world_size != 0:
                mulFactor = (
                    2 * (collectiveArgs.world_size - 1) / (collectiveArgs.world_size)
                )
            busBW = algBW * mulFactor
        elif collective in (
            "all_to_all",
            "all_to_allv",
            "gather",
            "all_gather",
            "reduce_scatter",
            "reduce_scatter_base",
            "scatter",
            "all_gather_base",
        ):
            if collectiveArgs.world_size != 0:
                mulFactor = (collectiveArgs.world_size - 1) / (
                    collectiveArgs.world_size
                )
            busBW = algBW * mulFactor
        elif collective in ("reduce", "broadcast", "incast", "multicast"):
            busBW = algBW
        else:
            logger.error(
                f"collective: {collective} is not supported in computing bus BW! "
            )
        return busBW

    def alloc_ones(
        self,
        sizeArr: int,
        curRankDevice: str = "cuda",
        dtype: torch.dtype = torch.float32,
        scaleFactor: float = 1.0,
    ) -> torch.Tensor:
        """
        Create a tensor filled with 1s of size sizeArr, and return the tensor multiplied by the scaleFactor.

        Args:
            sizeArr: Size of desired tensor.
            curRankDevice: Desired device of returned tensor.
            dtype: Datatype of returned tensor.
            scaleFactor: Factor to scale the returned tensor.
        Returns:
            ipTensor: Tensor filled with 1s.
        """
        ipTensor = torch.ones(sizeArr, device=curRankDevice, dtype=dtype)
        if scaleFactor != 1.0:
            ipTensor = ipTensor * scaleFactor
        return ipTensor

    def noop(
        self,
        collectiveArgs: collectiveArgsHolder = None,
        retFlag: bool = False,
        pair: bool = False,
    ) -> None:
        """no-op for the case we want to skip comms/compute"""
        pass

    @abstractmethod
    def sayHello(
        self, global_rank: int, local_rank: int, world_size: int, master_ip: str
    ) -> None:
        """Print startup information of the backend."""
        pass

    # Collectives, if you would like more detailed documentation about the behavior of these collectives, visit https://pytorch.org/docs/stable/_modules/torch/distributed/distributed_c10d.html.
    @abstractmethod
    def all_reduce(self, collectiveArgs: collectiveArgsHolder, retFlag: bool = False):
        pass

    @abstractmethod
    def reduce(self, collectiveArgs: collectiveArgsHolder, retFlag: bool = False):
        pass

    @abstractmethod
    def all_to_all(self, collectiveArgs: collectiveArgsHolder, retFlag: bool = False):
        pass

    @abstractmethod
    def all_to_allv(self, collectiveArgs: collectiveArgsHolder, retFlag: bool = False):
        pass

    @abstractmethod
    def complete_accel_ops(
        self, collectiveArgs: collectiveArgsHolder, initOp: bool = False
    ):
        pass

    @abstractmethod
    def barrier(self, collectiveArgs: collectiveArgsHolder, name: str = "dummy"):
        pass

    def sync_barrier(self, collectiveArgs: collectiveArgsHolder, desc: str = "world"):
        self.barrier(collectiveArgs, name=desc)

    @abstractmethod
    def get_reduce_op(self, opName: str):
        pass

    # Compute functions
    @abstractmethod
    def gemm(self, collectiveArgs: collectiveArgsHolder) -> None:
        pass

    # Memory related
    @abstractmethod
    def get_mem_size(self, collectiveArgs: collectiveArgsHolder) -> int:
        """Return memory size of current input tensor."""
        pass

    @abstractmethod
    def alloc_random(
        self,
        sizeArr: int,
        curRankDevice: str,
        dtype: torch.dtype,
        scaleFactor: float = 1.0,
    ) -> torch.Tensor:
        """Allocate tensor of random values according to parameters."""
        pass

    @abstractmethod
    def alloc_embedding_tables(
        self, n: int, m: int, curRankDevice: str, dtype: torch.dtype
    ):
        """Allocate embedding table based on parameters."""
        pass

    @abstractmethod
    def alloc_empty(
        self, sizeArr: int, dtype: torch.dtype, curRankDevice: str
    ) -> torch.Tensor:
        """Allocate tensor with uninitialized data based on parameters."""
        pass

    @abstractmethod
    def clear_memory(self, collectiveArgs: collectiveArgsHolder):
        """Clear memory in use by backend function."""
        pass

    # Getting world-size and other information.
    @abstractmethod
    def get_local_rank(self) -> int:
        pass

    @abstractmethod
    def get_global_rank(self) -> int:
        pass

    @abstractmethod
    def get_world_size(self) -> int:
        pass

    @abstractmethod
    def get_device(self) -> str:
        pass

    @abstractmethod
    def get_hw_device(self) -> str:
        pass

    @abstractmethod
    def get_default_group(self) -> ProcessGroup:
        pass

    @abstractmethod
    def get_groups(self) -> List[ProcessGroup]:
        pass

    @abstractmethod
    def get_num_pgs(self) -> int:
        pass

    # Init functions
    @abstractmethod
    def initialize_backend(
        self, master_ip: str, master_port: str, backend: str = "gloo"
    ) -> None:
        pass

    @abstractmethod
    def benchmark_comms(self) -> None:
        pass


class comms_world_info_holder:
    """Class holding communication-world related parameters."""

    def __init__(
        self,
        master_ip: str,
        master_port: str,
        num_tpu_cores: int,
        comms_env_params: Dict[str, int],
    ) -> None:
        self.global_rank = comms_env_params["global_rank"]
        self.local_rank = comms_env_params["local_rank"]
        self.world_size = comms_env_params["world_size"]

        self.master_ip = master_ip
        self.master_port = master_port
        self.num_tpu_cores = num_tpu_cores


class commsParamsHolderBase:
    """Class holding object for common input parameters"""

    def __init__(self, args: Namespace) -> None:
        self.nw_stack = args.nw_stack
        self.dtype = args.dtype
        self.backend = args.backend
        self.device = args.device
        self.blockingFlag = args.z
        # quantization
        self.bitwidth = args.bitwidth
        self.quant_a2a_embedding_dim = args.quant_a2a_embedding_dim
        self.quant_threshold = args.quant_threshold
        self.dcheck = args.c
        self.groupRanks = (
            {}
        )  # record what ranks each process group will work on {pg_id, ranks}
        self.use_ext_dist = args.use_ext_dist


class commsDlrmParamsHolder(commsParamsHolderBase):
    """Class holding object for the input parameters of DLRM benchmark."""

    def __init__(
        self,
        args,
        mpi_env_params: Dict[str:int],
    ) -> None:
        super().__init__(args)

        # extra DLRM parameters
        self.numDevices = mpi_env_params["world_size"]
        self.numBatches = args.num_batches + args.warmup_batches
        # NOTE: Should ensure that dataSize = int(N) * numDevices * batchSize
        self.numBatchesPerEpoch = args.mini_batch_size
        self.dataSize = (
            mpi_env_params["world_size"] * self.numBatches * self.numBatchesPerEpoch
        )
        self.embedLayers = []  # scaledEmbedLayers
        self.mini_batch_size = args.mini_batch_size
        self.arch_sparse_feature_size = args.arch_sparse_feature_size
        self.nw_stack = args.nw_stack
        self.warmup_batches = args.warmup_batches
        self.device = args.device
        self.backend = args.backend

        # additional parameters used in runBench()
        self.perf_debug = args.perf_debug
        self.print_comms = args.print_comms


class commsParamsHolder(commsParamsHolderBase):
    """Class holding object for the input parameters from collective benchmark."""

    def __init__(
        self,
        args,
        comms_world_info: comms_world_info_holder,
        element_size: int,
        benchTime: Callable,
    ) -> None:
        super().__init__(args)

        self.element_size = element_size
        self.sizes = args.ss
        self.beginSize = args.b
        self.endSize = args.e
        self.maxSize = int(args.e // self.element_size)
        self.inSplit = args.i
        self.outSplit = args.o
        self.data_type = args.data_type
        self.stepFactor = args.f
        self.stepBytes = args.sb
        self.srcOrDst = args.root
        self.quant_threshold = max(
            self.endSize, self.quant_threshold
        )  # use quantization for all sizes in collective benchmark

        self.numWarmupIters = args.w
        self.numIters = args.n
        self.collective = args.collective
        self.collective_list = args.collective.split(",")
        self.mode = args.mode

        self.kernel = args.kernel
        self.num_compute = args.num_compute
        self.num_coll = args.num_coll
        self.mm_dim = args.mm_dim
        self.emb_dim = args.emb_dim
        self.batch_size = args.batch_size
        self.num_embs = args.num_embs
        self.num_emb_tables_per_device = args.num_emb_tables_per_device
        self.num_emb_tables_batched = args.num_emb_tables_batched
        self.bag_size = args.bag_size
        self.benchTime = benchTime

        self.pair = args.pair
        self.collective_pair = args.collective_pair

        self.pt2pt = args.pt2pt
        self.window = args.window

        self.src_ranks = parseRankList(args.src_ranks, "src_ranks", comms_world_info)
        self.dst_ranks = parseRankList(args.dst_ranks, "dst_ranks", comms_world_info)
        self.comms_world_info = comms_world_info

        self.size_start_profiler = args.size_start_profiler


class collectiveArgsHolder:
    """Class holding object for all the parameters related to a collective operation/experiment."""

    def __init__(self) -> None:
        self.group = None
        self.groups = {}  # {pg_id, pg}
        self.num_pgs = 0
        self.device = {}
        self.world_size = 0
        self.data_type = ""

        self.numIters = 0
        self.numWarmupIters = 0
        self.global_rank = -1
        self.backendFuncs = {}
        self.collective = ""
        self.collectiveId = 0
        self.pt2pt = ""
        self.src_rank = -1
        self.dst_rank = -1

        self.MMout = {}
        self.MMin1 = {}
        self.MMin2 = {}
        self.MMin3 = {}
        self.numComputePerIter = 0
        self.numCollPerIter = 0
        self.batch_size = 0

        self.emb = None
        self.embRequests = None
        self.emb_dim = 0
        self.num_emb_tables_batched = -1
        self.num_emb_ops = 0
        self.BTBlockSize = {}
        self.LookupOut = {}

        self.ipTensor_split = []
        self.opTensor_split = []

        self.ipTensor = []
        self.opTensor = []
        self.srcOrDst = -1
        self.asyncOp = -1
        self.dataSize = 0
        self.numElements = 0
        self.waitObj = []
        self.waitObjIds = {}  # mapping of reqID to future of async collectives

        self.ipTensor_split_pair = []
        self.opTensor_split_pair = []

        self.ipTensor_pair = None
        self.opTensor_pair = None
        self.dataSize_pair = 0
        self.numElements_pair = 0

        self.all2all_qcomm = None
        self.reducescatter_allgather_qcomm = None
        self.allreduce_qcomm = 32  # TODO: set it as the bitwidth for now until the quantization kernels be supported
        self.reduce_qcomm = 32
        self.quant_threshold = 0
        self.quant_time = paramTimer()
        self.dequant_time = paramTimer()
        self.enable_profiler = False

        self.compute_stream = None
        self.use_ext_dist = False


class paramCommsBench(ABC):
    """Abstract class for any param comms benchmark."""

    def __init__(self, supportedNwstacks: List[str] = None) -> None:
        self.supportedNwstacks = supportedNwstacks
        self.supported_tpu_core_valuses = [1, 8]
        self.dtypeMap = {
            "float32": torch.float32,
            "int32": torch.int32,
            "long": torch.long,
            "float16": torch.half,
            "bfloat16": torch.bfloat16,
            "float64": torch.double,
            "bool": torch.bool,
            "Float": torch.float32,
            "Int": torch.int32,
            "Long": torch.long,
            "Double": torch.double,
            "Half": torch.half,
            "Bool": torch.bool,
            "Byte": torch.uint8,
        }
        self.supportedDtype = list(self.dtypeMap.keys())
        self.backendFuncs = ""
        self.collectiveArgs = collectiveArgsHolder()
        self.comm_size = 1
        self.global_rank = -1
        # update initVal to test different value
        self.initVal = 1

    def isCudaAvail(self) -> bool:
        return torch.cuda.is_available()

    def dcheck(
        self, commsParams: commsParamsHolderBase, curSize: int, tensor: torch.Tensor
    ) -> None:
        """ "
        Data validaton check for collectives, will raise an exception if invalid.

        Args:
            commsParams: Contains collective information.
            curSize: Current size in bytes.
            tensor: Tensor to validate.
        Returns:
            None
        """
        expRes = self.initVal
        if (
            commsParams.collective
            in (
                "all_reduce",
                "reduce_scatter",
                "reduce_scatter_base",
            )
        ) or (
            self.backendFuncs.get_global_rank() == commsParams.srcOrDst
            and commsParams.collective == "reduce"
        ):
            # NOTE: for sum op. and the inital value is "self.initVal", for boolean type, self.initVal is always True
            expRes = (
                self.initVal
                if tensor.dtype == torch.bool
                else self.collectiveArgs.world_size * self.initVal
            )

        if (
            # Check results for incast only on root
            commsParams.collective in ("incast", "reduce", "gather")
            and self.backendFuncs.get_global_rank() != commsParams.srcOrDst
        ) or (
            # Check results of multicast only for dst_ranks
            commsParams.collective in ("multicast", "pt2pt")
            and self.backendFuncs.get_global_rank() not in commsParams.dst_ranks
        ):
            return

        if isinstance(tensor, list):
            # for allgather and incast, it's a list of tensors:
            for (rank, t) in enumerate(tensor):
                if not torch.all(torch.eq(t, expRes)):
                    for (index, val) in enumerate(t):
                        if val != expRes:
                            raise ValueError(
                                f"[{curSize}-bytes {commsParams.collective}] Wrong value at [{rank}][{index}] = {t[index]}, expected {expRes}\n {tensor}"
                            )
        else:
            if not torch.all(torch.eq(tensor, expRes)):
                for (index, val) in enumerate(tensor):
                    if val != expRes:
                        raise ValueError(
                            f"[{curSize}-bytes {commsParams.collective}] Wrong value at [{index}] = {tensor[index]}, expected {expRes}\n {tensor}"
                        )

    def setTensorVal(self, tensor: torch.Tensor, useRandVal: bool = True) -> None:
        """
        Set tensor value, use initVal if useRandVal is false.

        Args:
            tensor: Tensor to set value on.
            useRandVal: Determines whether to use predictable values or not.
        Returns:
            None
        """
        newVal = random.random() if useRandVal else self.initVal
        t = tensor[0] if isinstance(tensor, list) else tensor
        if t.type == torch.bool:
            newVal = newVal > 0.5
        # reset values
        if self.collectiveArgs.collective in ("all_reduce", "reduce"):
            # all processes use initVal to have predictable results
            tensor[:] = self.initVal
        elif self.collectiveArgs.collective in ("broadcast", "multicast"):
            # root process uses initVal and others use random values
            tensor[:] = (
                self.initVal
                if (self.backendFuncs.get_global_rank() == self.collectiveArgs.srcOrDst)
                else newVal
            )
        elif isinstance(tensor, list):
            # could be a list of tensor, for all_gather, gather, reduce_scatter
            for t in tensor:
                t[:] = newVal
        else:
            tensor[:] = newVal

    # Collection of prepComm private methods. These methods prepare tensors for the respective collective.

    def _prep_all_to_allv(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):
        """Prepare the all_to_allv mode"""

        opTensor = []
        if allocate:
            # all_to_allv requires two tensors
            opTensor = self.backendFuncs.alloc_random(
                [numElementsOut], curDevice, dtype, scaleFactor
            )
        # all_to_allv requires tensors to specify split
        self.collectiveArgs.opTensor_split = (
            curComm.outSplit
            if (curComm.outSplit is not None)
            else [(numElementsOut // world_size) for _ in range(world_size)]
        )
        self.collectiveArgs.ipTensor_split = (
            curComm.inSplit
            if (curComm.inSplit is not None)
            else [(numElementsIn // world_size) for _ in range(world_size)]
        )
        return (ipTensor, opTensor)

    def _prep_all_to_all(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):
        # all_to_all requires two tensor lists, e.g., List[torch.Tensor]

        ipTensor = []
        opTensor = []
        if allocate:
            if commsParams.dcheck == 1:
                for _ in range(world_size):
                    ipTensor.append(
                        self.backendFuncs.alloc_ones(
                            [(numElementsIn // world_size)],
                            curDevice,
                            commsParams.dtype,
                            self.initVal,
                        )
                    )
            else:
                for _ in range(world_size):
                    ipTensor.append(
                        self.backendFuncs.alloc_random(
                            [(numElementsIn // world_size)],
                            curDevice,
                            commsParams.dtype,
                            scaleFactor,
                        )
                    )
            for _ in range(world_size):
                opTensor.append(
                    self.backendFuncs.alloc_random(
                        [(numElementsOut // world_size)], curDevice, dtype, scaleFactor
                    )
                )
        return (ipTensor, opTensor)

    def _prep_all_gather(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):
        opTensor = []

        if allocate:
            if commsParams.dcheck == 1:
                ipTensor = self.backendFuncs.alloc_ones(
                    [numElementsIn // world_size],
                    curDevice,
                    dtype,
                    scaleFactor=self.initVal,
                )
            else:
                ipTensor = self.backendFuncs.alloc_random(
                    [numElementsIn // world_size], curDevice, dtype, scaleFactor
                )
            # allgather requires a tensor list, e.g., List[torch.Tensor]
            for _ in range(world_size):
                opTensor.append(
                    self.backendFuncs.alloc_random(
                        [numElementsIn // world_size], curDevice, dtype, scaleFactor
                    )
                )
        return (ipTensor, opTensor)

    def _prep_all_gather_base(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):

        opTensor = []
        if allocate:
            if commsParams.dcheck == 1:
                ipTensor = self.backendFuncs.alloc_ones(
                    [numElementsIn // world_size],
                    curDevice,
                    dtype,
                    scaleFactor=self.initVal,
                )
            else:
                ipTensor = self.backendFuncs.alloc_random(
                    [numElementsIn // world_size], curDevice, dtype, scaleFactor
                )
            # this is a single all gather with flat output tensor
            opTensor = self.backendFuncs.alloc_random(
                numElementsIn,
                curDevice,
                dtype,
                scaleFactor,
            )
        return (ipTensor, opTensor)

    def _prep_incast(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):
        # incast requires a tensor list with length of src_ranks, e.g., List[torch.Tensor]
        opTensor = []

        if allocate:
            for _ in self.collectiveArgs.src_ranks:
                opTensor.append(
                    self.backendFuncs.alloc_random(
                        [numElementsOut], curDevice, dtype, scaleFactor
                    )
                )
        return (ipTensor, opTensor)

    def _prep_reduce_scatter(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):

        ipTensor = []
        opTensor = []
        if allocate:
            if commsParams.dcheck == 1:
                for _ in range(world_size):
                    ipTensor.append(
                        self.backendFuncs.alloc_ones(
                            [numElementsOut // world_size],
                            curDevice,
                            commsParams.dtype,
                            self.initVal,
                        )
                    )
            else:
                for _ in range(world_size):
                    ipTensor.append(
                        self.backendFuncs.alloc_random(
                            [numElementsOut // world_size],
                            curDevice,
                            commsParams.dtype,
                            scaleFactor,
                        )
                    )
            opTensor = self.backendFuncs.alloc_random(
                [numElementsOut // world_size], curDevice, dtype, scaleFactor
            )
        return (ipTensor, opTensor)

    def _prep_reduce_scatter_base(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):

        ipTensor = []
        opTensor = []
        if allocate:
            if commsParams.dcheck == 1:
                ipTensor = self.backendFuncs.alloc_ones(
                    numElementsOut,
                    curDevice,
                    commsParams.dtype,
                    self.initVal,
                )
            else:
                ipTensor = self.backendFuncs.alloc_random(
                    numElementsOut,
                    curDevice,
                    commsParams.dtype,
                    scaleFactor,
                )
            opTensor = self.backendFuncs.alloc_random(
                [numElementsOut // world_size], curDevice, dtype, scaleFactor
            )
        return (ipTensor, opTensor)

    def _prep_pt2pt(
        self,
        ipTensor: torch.tensor,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        numElementsIn: int,
        numElementsOut: int,
        world_size: int,
        curDevice: str,
        dtype: torch.dtype,
        scaleFactor: float,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):
        # pt2pt or out-of-place collectives
        opTensor = []
        if allocate:
            opTensor = self.backendFuncs.alloc_random(
                [numElementsOut],
                curDevice,
                dtype,
                scaleFactor,
            )
        return (ipTensor, opTensor)

    def prepComm(
        self,
        curComm: commsArgs,
        commsParams: commsParamsHolderBase,
        allocate: bool = True,
    ) -> (torch.Tensor, torch.Tensor):
        """
        Allocate the tensors for collective.

        Args:
            curComm: Current collective communication.
            commsParams: Holds parameters that affect tensor allocation.
        Returns:
            (iptensor, optensor): Appropriate input and output tensors for collective.
        """
        commOp = paramToCommName(
            curComm.comms if (curComm.comms is not None) else commsParams.collective,
            supported_comms=self.backendFuncs.collectiveFunc.keys(),
        )

        if commOp in ("wait", "barrier"):
            return ([], [])

        numElementsIn = curComm.inMsgSize
        # numElementsOut is only meaningful for out-of-place collectives and pt2pt
        numElementsOut = curComm.outMsgSize
        world_size = self.collectiveArgs.world_size
        dtype = commsParams.dtype
        curDevice = commsParams.device
        # scaleFactor = 1 if commsParams.collective == "all_to_all" else numElements * numElements
        scaleFactor = numElementsOut * numElementsOut
        opTensor = []

        if allocate:
            if commsParams.dcheck == 1:
                # use predictable values for data validation check
                ipTensor = self.backendFuncs.alloc_ones(
                    [numElementsIn], curDevice, dtype, scaleFactor=self.initVal
                )
            else:
                ipTensor = self.backendFuncs.alloc_random(
                    [numElementsIn], curDevice, dtype, scaleFactor
                )
        else:
            ipTensor = []
        # TODO: consider using this dictionary to check valid keywords rather than silently defaulting

        dispatchDict = {
            "all_to_allv": self._prep_all_to_allv,
            "all_to_all": self._prep_all_to_all,
            "all_gather": self._prep_all_gather,
            "gather": self._prep_all_gather,
            "all_gather_base": self._prep_all_gather_base,
            "incast": self._prep_incast,
            "reduce_scatter": self._prep_reduce_scatter,
            "reduce_scatter_base": self._prep_reduce_scatter_base,
            "scatter": self._prep_reduce_scatter,
            "pt2pt": self._prep_pt2pt,
        }

        function_to_call = dispatchDict.get(commOp)
        if function_to_call is not None:
            ipTensor, opTensor = function_to_call(
                ipTensor,
                curComm,
                commsParams,
                numElementsIn,
                numElementsOut,
                world_size,
                curDevice,
                dtype,
                scaleFactor,
                allocate,
            )
        else:
            # in-place case for other collectives such as allreduce, reduce, broadcast
            opTensor = ipTensor

        return (ipTensor, opTensor)

    @abstractmethod
    def runBench(self, *args, **kwargs) -> None:
        """Must override to start the desired benchmarking"""
        pass

    @abstractmethod
    def benchTime(self, *args, **kwargs) -> None:
        """Must override to run the desired benchmarking"""
        pass

    @abstractmethod
    def reportBenchTime(self, *args, **kwargs) -> None:
        """Must override to report/print the desired output"""
        pass

    @abstractmethod
    def readArgs(self, parser: ArgumentParser) -> None:
        """Basic/Common arguments for all PARAM-Comm benchmarks"""
        parser.add_argument(
            "--master-ip",
            type=str,
            default=default_master_ip
            if "MASTER_ADDR" not in os.environ
            else os.environ["MASTER_ADDR"],
            help="The master-IP to coordinate for Pytorch distributed stack",
        )  # The master-IP to coordinate.
        parser.add_argument(
            "--master-port",
            type=str,
            default=default_master_port
            if "MASTER_PORT" not in os.environ
            else os.environ["MASTER_PORT"],
            help="The master-port to coordinate for Pytorch distributed stack",
        )  # The master-port to coordinate.
        parser.add_argument(
            "--nw-stack",
            type=str,
            default="pytorch-dist",
            help="network stack to be used, supports " + str(self.supportedNwstacks),
        )  # The network stack to profile.
        parser.add_argument(
            "--dtype", type=torch.dtype, default=torch.float32
        )  # will be overwritten based on args.data_types and dtypeMap.
        parser.add_argument(
            "--num-tpu-cores",
            type=int,
            default=1,
            help="number of TPU cores to be used",
        )  # number of TPU cores
        parser.add_argument(
            "--log",
            type=str,
            default="ERROR",
            help="Logging level",
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        )  # logging level
        parser.add_argument(
            "--device",
            type=str,
            default=("cuda" if self.isCudaAvail() else "cpu"),
            choices=["cpu", "cuda", "rocm", "tpu"],
            help="data placement",
        )  # device to place data for collective benchmarking
        parser.add_argument(
            "--backend",
            type=str,
            default=("nccl" if self.isCudaAvail() else "gloo"),
            help="The backend to be used in PyTorch distributed process group",
            choices=["nccl", "gloo", "mpi", "ucc", "xla", "fairring"],
        )  #  backend used for the network stack
        parser.add_argument(
            "--z",
            type=int,
            default=0,
            help="use blocking/non-blocking mode for collectives",
            choices=[0, 1],
        )  # 'sync/blocking' : 1 , 'async/non-blocking' : 0
        parser.add_argument(
            "--bitwidth",
            type=int,
            default=32,
            help="Quantization bitwidth",
            choices=[2, 4, 8, 16, 32],
        )  # comms quantization
        parser.add_argument(
            "--quant-a2a-embedding-dim",
            type=int,
            default=32,
            help="Embedding dimension used by quantization alltoall if enabled",
            choices=[32, 64, 128, 256],
        )  # Row dimension for quantization
        parser.add_argument(
            "--quant-threshold",
            type=int,
            default=33554432,
            help="threshold of message sizes to perform quantization if enabled",
        )  # quantization threshold, default 32 MB
        parser.add_argument(
            "--c",
            type=int,
            default=0,
            help="enable data validation check",
            choices=[0, 1],
        )  # validation check
        parser.add_argument(
            "--use-ext-dist",
            "--use-ext-pg",
            action="store_true",
            default=False,
            help="use extend_distributed wrapper",
        )  # use extend_distributed wrapper to init and create PGs
        pass

    @abstractmethod
    def checkArgs(self, args: Namespace) -> None:
        """Validate some basic/common arguments for all PARAM-Comm benchmarks"""
        if args.nw_stack not in self.supportedNwstacks:
            logger.error(
                f"Specified backend: {args.nw_stack} is not one of the supported backends: {str(self.supportedNwstacks)}. Make sure the input is using the correct case."
            )
            gracefulExit()
        if args.num_tpu_cores not in self.supported_tpu_core_valuses:
            logger.error(
                f"TPU core value: {args.num_tpu_cores} is not one of the supported values: {self.supported_tpu_core_valuses}"
            )
            gracefulExit()
        # check and set log level
        numeric_level = getattr(logging, args.log.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f"Invalid log level: {args.log}")
        comms_env_params = read_comms_env_vars()
        logging.basicConfig(
            level=numeric_level,
            format="[%(asctime)s][%(name)s][%(levelname)s][Rank{:3}] - %(message)s".format(
                comms_env_params["global_rank"]
            ),
        )
        # check master-ip and master-port with the following logic
        #   1) prefer the values passed to PARAM, i.e., through --master-ip and --master-port
        #   2) check and use the env. variable, i.e., MASTER_ADDR and MASTER_PORT
        #   3) if both #1 and #2 are not set, pre-defined default values will be used
        if "MASTER_ADDR" in os.environ:
            if args.master_ip not in (default_master_ip, os.environ["MASTER_ADDR"]):
                logger.warning(
                    f"--master-ip={args.master_ip} while MASTER_ADDR={os.environ['MASTER_ADDR']}, "
                    f"use --master-ip={args.master_ip} and continue..."
                )
                os.environ["MASTER_ADDR"] = args.master_ip
            else:
                logger.info(
                    "From environment variables, using MASTER_ADDR="
                    + os.environ["MASTER_ADDR"]
                )
        else:
            os.environ["MASTER_ADDR"] = args.master_ip

        if "MASTER_PORT" in os.environ:
            if args.master_port not in (default_master_port, os.environ["MASTER_PORT"]):
                logger.warning(
                    f"--master-port={args.master_port} while MASTER_PORT={os.environ['MASTER_PORT']}, "
                    f"use --master-port={args.master_port} and continue..."
                )
                os.environ["MASTER_PORT"] = args.master_port
            else:
                logger.info(
                    "From environment variables, using MASTER_PORT="
                    + os.environ["MASTER_PORT"]
                )
        else:
            os.environ["MASTER_PORT"] = args.master_port


def init_emb_lookup(collectiveArgs, commsParams, backendFuncs):
    """
    Initialize embedding table op

    Args:
        collectiveArgs: collective arguments.
        commsParams: Holds parameters that affect tensor allocation.
        backendFuncs: backend function
    Returns:
        None
    """
    try:
        # fbgemm_gpu can be downloaded from https://github.com/pytorch/FBGEMM/tree/main/fbgemm_gpu
        from fbgemm_gpu.split_embedding_utils import generate_requests

        from fbgemm_gpu.split_table_batched_embeddings_ops import (
            ComputeDevice,
            EmbeddingLocation,
            OptimType,
            SplitTableBatchedEmbeddingBagsCodegen,
        )
    except ImportError:
        logger.error("benchmarking with emb_lookup kernels requires fbgemm_gpu library")
        return
    collectiveArgs.emb_dim = commsParams.emb_dim
    num_embeddings = commsParams.num_embs
    collectiveArgs.batch_size = commsParams.batch_size
    num_tables_per_device = commsParams.num_emb_tables_per_device
    collectiveArgs.num_emb_tables_batched = commsParams.num_emb_tables_batched
    bag_size = commsParams.bag_size

    num_emb_tables_batched = (
        num_tables_per_device
        if collectiveArgs.num_emb_tables_batched == -1
        else collectiveArgs.num_emb_tables_batched
    )
    collectiveArgs.num_emb_ops = num_tables_per_device // num_emb_tables_batched

    collectiveArgs.emb = [
        SplitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    num_embeddings,
                    collectiveArgs.emb_dim,
                    EmbeddingLocation.DEVICE
                    if commsParams.device == "cuda"
                    else EmbeddingLocation.HOST,
                    ComputeDevice.CUDA
                    if commsParams.device == "cuda"
                    else ComputeDevice.CPU,
                )
                for _ in range(num_emb_tables_batched)
            ],
            device=backendFuncs.get_device(),
            optimizer=OptimType.EXACT_ROWWISE_ADAGRAD,
        )
        for _ in range(collectiveArgs.num_emb_ops)
    ]

    collectiveArgs.embRequests = generate_requests(
        iters=collectiveArgs.num_emb_ops,
        B=collectiveArgs.batch_size,
        T=num_emb_tables_batched,
        L=bag_size,
        E=num_embeddings,
    )
