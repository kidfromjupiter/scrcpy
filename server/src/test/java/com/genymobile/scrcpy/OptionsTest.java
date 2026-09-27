package com.genymobile.scrcpy;

import org.junit.Assert;
import org.junit.Test;

public class OptionsTest {

    @Test
    public void testCameraExposureOptions() {
        Options options = Options.parse(BuildConfig.VERSION_NAME, "camera_exposure=-1.5");

        Assert.assertEquals(-1.5f, options.getCameraExposure(), 0);
    }

    @Test
    public void testCameraManualExposureOptions() {
        Options options = Options.parse(BuildConfig.VERSION_NAME, "camera_shutter=10000000", "camera_iso=400");

        Assert.assertEquals(10000000L, options.getCameraShutter());
        Assert.assertEquals(400, options.getCameraIso());
    }

    @Test(expected = IllegalArgumentException.class)
    public void testInvalidCameraShutter() {
        Options.parse(BuildConfig.VERSION_NAME, "camera_shutter=0");
    }

    @Test(expected = IllegalArgumentException.class)
    public void testInvalidCameraIso() {
        Options.parse(BuildConfig.VERSION_NAME, "camera_iso=0");
    }

    @Test(expected = IllegalArgumentException.class)
    public void testInvalidCameraExposure() {
        Options.parse(BuildConfig.VERSION_NAME, "camera_exposure=NaN");
    }
}
