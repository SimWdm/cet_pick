import numpy as np
import numpy.fft as fft
import pandas as pd
import mrcfile
import warnings

import argparse

from cet_pick.utils.filter_utils import rad_avg, rot_kernel


def match_spectrum(tomo, target_spectrum, cutoff=None, smooth=0):
    
    target_spectrum = target_spectrum.copy()
    tomo -= tomo.min()
    tomo /= tomo.max()

    t = fft.fftn(tomo)
    t = fft.fftshift(t)

    del tomo

    input_spectrum = rad_avg(np.abs(t))
    target_spectrum.resize(len(input_spectrum))
    equal_v = target_spectrum / input_spectrum

    if cutoff:
        if smooth:
            slope = len(equal_v)/smooth
            offset = 2 * slope * ((cutoff - len(equal_v) / 2) / len(equal_v))
        
            cutoff_v = 1/(1 + np.exp(np.linspace(-slope, slope, len(equal_v)) - offset))

        else:
            cutoff_v = np.ones_like(equal_v)
            try:
                equal_v[cutoff:] = 0
            except IndexError:
                warnings.warn("Flat cutoff is higher than maximum frequency")
            
        equal_v *= cutoff_v

    equal_kernel = rot_kernel(equal_v, t.shape)
    
    t *= equal_kernel
    del equal_kernel
    
    t = fft.ifftn(t)
    t = np.abs(t).astype("f4")

    return t


def main():
    
    parser = get_cli()
    args = parser.parse_args()
    
    with mrcfile.open(args.input, permissive = True) as m:
        tomo = m.data.astype("f4")
        tomo_h = m.header

    target_spectrum = pd.read_csv(args.target, sep="\t")["intensity"].values

    filtered_tomo = match_spectrum(tomo, target_spectrum, args.cutoff, args.smoothen)

    m = mrcfile.new(args.output, overwrite=True)
    m.set_data(filtered_tomo)
    m.set_extended_header(tomo_h)
    m.close()


def get_cli():
    parser = argparse.ArgumentParser(
        description="Match tomogram to another tomogram's amplitude spectrum."
    )

    parser.add_argument( 
        "-i",
        "--input",
        required=True,
        help="Tomogram to match (.mrc/.rec)"
    )

    parser.add_argument( 
        "-t",
        "--target",
        required=True,
        help="Target spectrum to match the input tomogram to (.tsv)"
    )

    parser.add_argument( 
        "-o",
        "--output",
        required=True,
        help="Output location for matched tomogram"
    )

    parser.add_argument( 
        "-c",
        "--cutoff",
        required=False,
        default=False,
        type=int,
        help="Lowpass cutoff to apply"
    )

    parser.add_argument( 
        "-s",
        "--smoothen",
        required=False,
        default=0,
        type=float,
        help="Smoothening to apply to lowpass filter. Value roughly resembles sigmoid width in pixels"
    )
    
    return parser


if __name__ == "__main__":
    main()

