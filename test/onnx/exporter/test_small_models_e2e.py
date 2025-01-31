# Owner(s): ["module: onnx"]
"""Unit tests for the onnx dynamo exporter."""

from __future__ import annotations

import logging

import torchvision
import transformers

import torch
from torch.onnx._internal.exporter import _testing as onnx_testing
from torch.testing._internal import common_utils
from torch.utils import _pytree as torch_pytree


@common_utils.instantiate_parametrized_tests
class DynamoExporterTest(common_utils.TestCase):
    def export(self, model, args=(), kwargs=None, **options) -> torch.onnx.ONNXProgram:
        onnx_program = torch.onnx.export(
            model, args, kwargs=kwargs, dynamo=True, fallback=False, **options
        )
        assert onnx_program is not None
        return onnx_program

    def test_insert_contiguous_between_transpose_and_view(self):
        class Model(torch.nn.Module):
            def forward(self, query, key, value):
                res = torch.nn.functional.scaled_dot_product_attention(
                    query, key, value
                )
                rest = res.transpose(0, 1)
                return rest.view(8, 32, 128 * 64)

        model = Model()

        query = torch.rand(32, 8, 128, 64, dtype=torch.float16)
        key = torch.rand(32, 8, 128, 64, dtype=torch.float16)
        value = torch.rand(32, 8, 128, 64, dtype=torch.float16)

        ep = torch.export.export(model, (query, key, value), strict=False)
        self.assertNotIn("call_method", str(ep.graph))

        onnx_program = self.export(model, (query, key, value))
        onnx_testing.assert_onnx_program(onnx_program, atol=1e-3, rtol=1)

    def test_constant_complex(self):
        class MulModule(torch.nn.Module):
            def forward(self, x):
                y = 2 + 3j
                return torch.ops.aten.mul(x, y)

        # Example usage with complex inputs
        x = torch.tensor(
            [[1.0 + 2.0j, 3.0 + 4.0j], [5.0 + 6.0j, 7.0 + 8.0j]], dtype=torch.complex64
        )

        onnx_program = self.export(MulModule(), (x,))
        onnx_testing.assert_onnx_program(onnx_program)

    def test_pow_does_not_trigger_type_promotion(self):
        class Model(torch.nn.Module):
            def forward(self, x):
                return x**2.0

        x = torch.tensor([1.0, 2.0, 3.0], dtype=torch.float16)

        onnx_program = self.export(Model(), (x,))
        onnx_testing.assert_onnx_program(onnx_program)
        self.assertNotIn("Cast", [node.op_type for node in onnx_program.model.graph])

    def test_onnx_export_control_flow(self):
        class CondModel(torch.nn.Module):
            def forward(self, x):
                def true_fn(x):
                    return x + 1.0

                def false_fn(x):
                    return x - 42.0

                y = torch.cond(x.sum() > 0, true_fn, false_fn, [x])
                return y

        onnx_program = self.export(CondModel(), (torch.tensor([1, 2]),))
        onnx_model = onnx_program.model
        self.assertIn("If", [node.op_type for node in onnx_model.graph])
        onnx_testing.assert_onnx_program(onnx_program)
        # Test different branches
        onnx_testing.assert_onnx_program(onnx_program, args=(torch.tensor([-1, -2]),))

    def test_onnx_export_nested_control_flow_and_nested_weights(self):
        class Submodule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                # Nested weight
                self.weight = torch.nn.Parameter(torch.tensor([100.0]))

            def forward(self, x):
                def true_fn(x):
                    return x * self.weight

                def false_fn(x):
                    return x / self.weight

                y = torch.cond(x.sum() <= 0, true_fn, false_fn, [x])
                return y

        class CondModel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.submodule = Submodule()
                self.weight = torch.nn.Parameter(torch.tensor([42.0]))

            def forward(self, x):
                def true_fn(x):
                    return self.submodule(x - self.weight)

                def false_fn(x):
                    return x - self.weight

                y = torch.cond(x.sum() > 0, true_fn, false_fn, [x])
                return y

        onnx_program = self.export(CondModel(), (torch.tensor([1, 2]),))
        onnx_testing.assert_onnx_program(onnx_program)
        onnx_testing.assert_onnx_program(onnx_program, args=(torch.tensor([0, 0]),))
        onnx_testing.assert_onnx_program(onnx_program, args=(torch.tensor([43, 43]),))

    def test_onnx_export_control_flow_multi_outputs(self):
        class CondModel(torch.nn.Module):
            def forward(self, x):
                z = torch.ones_like(x)

                def true_fn(x, z):
                    x = x + 1.0
                    z = z * 1.0
                    return x, z

                def false_fn(x, z):
                    x = x - 1.0
                    z = z * 0.0
                    return x, z

                x = torch.cond(x.sum() > 0, true_fn, false_fn, (x, z))
                return x, z

        onnx_program = torch.onnx.export(
            CondModel(),
            (torch.tensor([1, 2]),),
            dynamo=True,
            fallback=False,
        )
        onnx_testing.assert_onnx_program(onnx_program)
        onnx_testing.assert_onnx_program(onnx_program, args=(torch.tensor([-1, -2]),))

    def test_onnx_export_torchvision_ops(self):
        class VisionModel(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, *x):
                out = torchvision.ops.nms(x[0], x[1], x[2])
                return out

        args = (
            torch.tensor([[0, 0, 1, 1], [0.5, 0.5, 1, 1]], dtype=torch.float),
            torch.tensor([0.1, 0.2]),
            0,
        )
        onnx_program = self.export(VisionModel(), args)
        onnx_testing.assert_onnx_program(onnx_program)

    def test_empty(self):
        def func(x):
            return torch.empty(x.size(), dtype=torch.int64)

        # Since `torch.empty` returns tensor with uninitialized data, we cannot
        # test this under `test_fx_to_onnx_with_onnxruntime.py` with result comparison.
        _ = self.export(func, (torch.randn(1, 2),))

    def test_multiple_outputs_op_with_evaluator(self):
        class TopKModel(torch.nn.Module):
            def forward(self, x):
                values, _ = torch.topk(x, 3)
                return torch.sum(values)

        onnx_program = self.export(
            TopKModel(), (torch.arange(1.0, 6.0, requires_grad=True),)
        )
        onnx_testing.assert_onnx_program(onnx_program)

    def test_exported_program_torch_distributions_normal_Normal(self):
        class Model(torch.nn.Module):
            def __init__(self) -> None:
                self.normal = torch.distributions.normal.Normal(0, 1)
                super().__init__()

            def forward(self, x):
                return self.normal.sample(x.shape)

        with torch.no_grad():
            exported_program = torch.export.export(
                Model(), args=(torch.randn(2),), strict=False
            )
            _ = self.export(exported_program)

    @common_utils.parametrize(
        "float8_type",
        [
            common_utils.subtest(
                torch.float8_e5m2,
                name="torch_float8_e5m2",
            ),
            common_utils.subtest(
                torch.float8_e5m2fnuz,
                name="torch_float8_e5m2fnuz",
            ),
            common_utils.subtest(
                torch.float8_e4m3fn,
                name="torch_float8_e4m3fn",
            ),
            common_utils.subtest(
                torch.float8_e4m3fnuz,
                name="torch_float8_e4m3fnuz",
            ),
        ],
    )
    def test_float8_support(self, float8_type):
        class Float8Module(torch.nn.Module):
            def forward(self, input: torch.Tensor):
                input = input.to(float8_type)
                return input

        _ = self.export(Float8Module(), (torch.randn(1, 2),))

    def test_export_with_logging_logger(self):
        logger = logging.getLogger(__name__)

        class LoggingLoggerModule(torch.nn.Module):
            def forward(self, x):
                logger.log("abc")
                return x + 1

        onnx_program = self.export(LoggingLoggerModule(), (torch.tensor(1),))
        onnx_testing.assert_onnx_program(onnx_program)

    def test_export_with_hf_logging_logger(self):
        logger = transformers.utils.logging.get_logger(__name__)

        class HFLoggingLoggerModule(torch.nn.Module):
            def forward(self, x):
                logger.warning_once("abc")
                return x + 1

        onnx_program = self.export(HFLoggingLoggerModule(), (torch.tensor(1),))
        onnx_testing.assert_onnx_program(onnx_program)

    def test_export_with_print(self):
        class PrintModule(torch.nn.Module):
            def forward(self, x):
                print("abc")
                return x + 1

        onnx_program = self.export(PrintModule(), (torch.tensor(1),))
        onnx_testing.assert_onnx_program(onnx_program)

    def test_export_with_dynamic_input(self):
        class Model(torch.nn.Module):
            def forward(self, x):
                return x + 1.0

        dim0 = torch.export.Dim("dim0")
        onnx_program = self.export(
            Model(),
            (torch.randn(2, 3, 4, dtype=torch.float),),
            dynamic_shapes=({0: dim0},),
        )

        onnx_testing.assert_onnx_program(
            onnx_program, args=(torch.randn(3, 3, 4, dtype=torch.float),)
        )

    def test_export_with_specialized_input_during_tracing(self):
        class Model(torch.nn.Module):
            def forward(self, x, y):
                return x + y

        dim0_x = torch.export.Dim("dim0_x", min=6)
        dynamic_shapes = {"x": {0: dim0_x}, "y": None}
        # specialized input y to 5 during tracing
        onnx_program = self.export(
            Model(),
            (
                torch.ones(7, 5),
                5,
            ),
            dynamic_shapes=dynamic_shapes,
        )

        onnx_testing.assert_onnx_program(onnx_program, args=(torch.ones(8, 5), 5))

    def test_export_with_none_arg_name_in_dynamic(self):
        class Model(torch.nn.Module):
            def forward(self, a, b):
                return a.sum() + b.sum()

        dim = torch.export.Dim("dim")
        onnx_program = self.export(
            Model(),
            (
                torch.randn(4, 4),
                torch.randn(4, 4),
            ),
            dynamic_shapes=(None, {0: dim}),
        )

        test_inputs = (
            torch.randn(4, 4),
            torch.randn(7, 4),
        )
        onnx_testing.assert_onnx_program(onnx_program, args=test_inputs)

    def test_export_with_non_arg_name_with_kwarg(self):
        class Model(torch.nn.Module):
            def forward(self, a, b, kw1, kw2):
                return a.sum() + b.sum() + kw1.sum() - kw2.sum()

        dim = torch.export.Dim("dim")
        dim_for_kw1 = torch.export.Dim("dim_for_kw1")
        onnx_program = self.export(
            Model(),
            (torch.randn(4, 4), torch.randn(4, 4)),
            {"kw2": torch.ones(4, 4), "kw1": torch.zeros(4, 4)},
            # We are specifying dynamism on the first kwarg even though user passed in
            # different order
            dynamic_shapes=(None, {0: dim}, {0: dim_for_kw1}, None),
        )

        # This should work even if the kwarg order are flipped.
        onnx_testing.assert_onnx_program(
            onnx_program,
            args=(torch.randn(4, 4), torch.randn(7, 4)),
            kwargs={"kw2": torch.ones(4, 4), "kw1": torch.zeros(9, 4)},
        )

    def test_export_with_input_lifting_buffers_mutation(self):
        for persistent in (True, False):

            class CustomModule(torch.nn.Module):
                def __init__(self) -> None:
                    super().__init__()
                    self.register_buffer(
                        "my_buffer", torch.tensor(4.0), persistent=persistent
                    )

                def forward(self, x, b):
                    output = x + b
                    (
                        self.my_buffer.add_(1.0) + 3.0
                    )  # Mutate buffer through in-place addition
                    return output

            dim = torch.export.Dim("dim")
            onnx_program = self.export(
                CustomModule(),
                (
                    torch.rand((3, 3), dtype=torch.float32),
                    torch.randn(3, 3),
                ),
                dynamic_shapes=({0: dim}, {0: dim}),
            )

            onnx_testing.assert_onnx_program(
                onnx_program,
                args=(torch.rand((4, 3), dtype=torch.float32), torch.randn(4, 3)),
            )

    def test_export_with_non_arg_name_with_container_type(self):
        class Model(torch.nn.Module):
            def forward(self, a, b):
                return a[0].sum() + a[1].sum() + b.sum()

        count = 0

        def dynamify_inp(x):
            # Mark the second input a[1] dynamic
            nonlocal count
            if count == 1:
                dim = torch.export.Dim("dim", min=3)
                count += 1
                return {0: dim}
            count += 1
            return None

        dynamic_shapes = torch_pytree.tree_map(
            dynamify_inp,
            (
                (torch.randn(4, 4), torch.randn(4, 4)),
                torch.randn(4, 4),
            ),
        )
        onnx_program = self.export(
            Model(),
            (
                (torch.randn(4, 4), torch.randn(4, 4)),
                torch.randn(4, 4),
            ),
            dynamic_shapes=dynamic_shapes,
        )

        # NOTE: Careful with the input format. The input format should be
        # consistent with how the model is exported.
        onnx_testing.assert_onnx_program(
            onnx_program,
            args=((torch.randn(4, 4), torch.randn(6, 4)), torch.randn(4, 4)),
        )

    def test_export_with_lazy_module_kwargs(self):
        class LazyModule(torch.nn.modules.lazy.LazyModuleMixin, torch.nn.Module):
            def initialize_parameters(self, *args, **kwargs):
                pass

            def forward(self, x, y):
                return x + y

        m = LazyModule()
        dim = torch.export.Dim("dim")
        dynamic_shapes = ({0: dim}, {0: dim})
        onnx_program = self.export(
            m,
            (),
            {"x": torch.randn(3, 3), "y": torch.randn(3, 3)},
            dynamic_shapes=dynamic_shapes,
        )

        inputs = {"x": torch.randn(6, 3), "y": torch.randn(6, 3)}
        onnx_testing.assert_onnx_program(onnx_program, kwargs=inputs)


if __name__ == "__main__":
    common_utils.run_tests()
